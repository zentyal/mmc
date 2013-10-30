# -*- coding: utf-8; -*-
#
# (c) 2013 Mandriva, http://www.mandriva.com/
#
# This file is part of Pulse 2, http://pulse2.mandriva.org
#
# Pulse 2 is free software; you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation; either version 2 of the License, or
# (at your option) any later version.
#
# Pulse 2 is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Pulse 2; if not, write to the Free Software
# Foundation, Inc., 51 Franklin Street, Fifth Floor, Boston,
# MA 02110-1301, USA.

import logging
import inspect
import random
import time
import datetime

from twisted.internet.defer import Deferred, maybeDeferred, DeferredList
from twisted.internet.threads import deferToThread, blockingCallFromThread
from twisted.internet.task import LoopingCall
from twisted.internet import reactor

from pulse2.consts import PULSE2_SUCCESS_ERROR
from pulse2.utils import SingletonN, extractExceptionMessage
from pulse2.network import NetUtils
from pulse2.scheduler.queries import CoHQuery, get_cohs, is_command_in_valid_time
from pulse2.scheduler.utils import PackUtils 
from pulse2.utils import extractExceptionMessage
from pulse2.scheduler.utils import chooseClientNetwork, launcher_proxymethod
from pulse2.scheduler.config import SchedulerConfig
from pulse2.scheduler.balance import ParabolicBalance, randomListByBalance, getBalanceByAttempts
from pulse2.scheduler.xmlrpc import getProxy
from pulse2.scheduler.launchers_driving import RemoteCallProxy
from pulse2.scheduler.checks import getCheck, getAnnounceCheck
from pulse2.scheduler.utils import getClientCheck, getServerCheck

from pulse2.database.msc.orm.commands_history import CommandsHistory

SchedulerConfig().setup("/etc/mmc/pulse2/scheduler/scheduler.ini")


def enum(*args, **kwargs):
    return type('Enum', 
                (), 
                dict((y, x) for x, y in enumerate(args), **kwargs)
               ) 

DIRECTIVE = enum("GIVE_UP", 
                 "PERFORM", 
                 "NEXT", 
                 "OVER_TIMED",
                 "FAILED",
                 "KILLED",
                 )
CC_STATUS = enum("ACTIVE",
                 "WAITING"
                 )


class PhaseProxyMethodContainer :
    """

    """

    _register_only = False
    _proxy_methods = {}


    @property
    def proxy_methods(self):
        return self._proxy_methods

    def register(self):
        self._register_only = True
        for name in dir(self) :
            fnc = getattr(self, name)

            if not hasattr(fnc, "is_proxy_fnc"): continue
            if not callable(fnc) : continue
            if fnc.is_proxy_fnc :
                args, vargs, kwds, defaults = inspect.getargspec(fnc)
                fnc(self, *args)


        self._register_only = True
 
        
class PhaseBase (PhaseProxyMethodContainer):

    name = None
    state_name = None

    coh = None
    cmd = None
    target = None
    
    phase = None

    host = None
    launchers_provider = None
    dispatcher = None
    last_activity_time = None

    def __init__(self, cohq=None, host=None):
        self.register()
        
        self.logger = logging.getLogger()
        self.host = host
        if cohq:
            self.set_cohq(cohq)

    def set_cohq(self, cohq):

        if not isinstance(cohq, CoHQuery):
           raise TypeError("Not CoHQuery type")

        self.coh = cohq.coh
        self.cmd = cohq.cmd
        self.target = cohq.target

        self.phase = cohq.get_phase(self.name)

    def _apply_initial_rules(self):
        if not self.cmd.in_valid_time(): 
            return DIRECTIVE.OVER_TIMED
 
        if self.coh.is_out_of_attempts() and self.phase.is_failed():
            self.coh.setStateFailed()
            return DIRECTIVE.KILLED
 
        self.logger.debug("Circuit #%s: %s phase" % (self.coh.id, self.name))

        if self.phase.is_done(): 
            # phase has already already done, jump to next phase
            self.logger.debug("command_on_host #%s: %s done" % (self.coh.id, self.name))
            return self.next()
        if self.phase.is_running(): 
            # phase still running, immediately returns, do nothing
            self.logger.debug("command_on_host #%s: %s still running" % (self.coh.id, self.name))
            #return self.give_up() # XXX - !!!!!!!!!!!!!!!!!!!!!!!
        return DIRECTIVE.PERFORM

    def _switch_on(self):
        self.phase.set_running()
        if not self.state_name :
            self.state_name = self.name
        self.update_history_in_progress()
        return DIRECTIVE.PERFORM


    def apply_initial_rules(self):
        ret = self._apply_initial_rules()
        if ret not in (DIRECTIVE.NEXT,
                       DIRECTIVE.GIVE_UP, 
                       DIRECTIVE.KILLED, 
                       DIRECTIVE.OVER_TIMED) :
            return self._switch_on()
        return ret


    def run(self):
        """ 
        Method to be overriden, but always returning perform() method.
        
        Contains usually a command state checks and eventual shortcuts
        to final or give_up phases.

        """
        try:
            if self.apply_initial_rules():
                return self.coh
        except:
            self.logger.error("flags or rules error \033[31m %s\033[0m"  % traceback.format_exc())
        return self.perform()

    def perform(self):
        pass

    def next(self):
        return DIRECTIVE.NEXT 

    def give_up(self):
        """ Releasing the circuit. """
        raise NotImplementedError

    def failed(self):
        raise NotImplementedError

    def calc_next_attempt_delay(self):
        """
        Schedules the next deploiment attempt.

        This calcul is based on statistics of previous failed attempts.
        The frequency of attempts has a parabolic progression, on other words
        the occurence of attempts is more frequently on start and end 
        of the lifecycle of command.
        """
        try :

            #self.logger.info("\033[33mStart the delay calculation for CoH: %s\033[0m" % self.coh.id)

            attempts_total = self.coh.attempts_left
            self.logger.debug("Number of failed attempts %d / %d" % (self.coh.attempts_failed, attempts_total))

            start_timestamp = time.mktime(self.cmd.start_date.timetuple())
            end_timestamp = time.mktime(self.cmd.end_date.timetuple())

            total_secs = end_timestamp - start_timestamp
            # ---------* just for debug display *----------------- 
            # TODO - determine DEBUG level from log.FileHandler...
            self.logger.debug("Execution plan for CoH %s :" %str(self.coh.id))
            _exec_plan = ParabolicBalance(attempts_total)
            _deltas = map(lambda x: x * total_secs, _exec_plan.balances)
            _next = start_timestamp
            for _attempt_nbr, _delta in enumerate(_deltas) :
                _next += _delta
                _nxt_date = datetime.datetime.fromtimestamp(_next).strftime("%Y-%m-%d %H:%M:%S")
                self.logger.debug("- next date : %s" % str(_nxt_date))
            # -----------------------------------------------------
            if self.coh.attempts_failed +1 <= attempts_total :
                b = ParabolicBalance(attempts_total)
                coef = b.balances[self.coh.attempts_failed]
                delay_in_seconds = coef * total_secs
            else :
                return 0

            delay = delay_in_seconds // 60
            self.logger.debug("Next delay for CoH %s : + %s min" %(str(self.coh.id),str(delay)))
            #self.logger.info("\033[33mNext delay for CoH %s : + %s min\033[0m" % (str(self.coh.id),str(delay)))
            return delay
            #if self.cmd.getNextConnectionDelay() != delay :
            #    self.cmd.setNextConnectionDelay(delay)
            #self.coh.

        except:
            self.logger.error("\033[31m calc next delay err:%s\033[0m"  % traceback.format_exc())

class Phase (PhaseBase):

    def got_error_in_error(self, reason):

        logging.getLogger().error("Circuit #%s: got an error within an error: %s" %
                (self.coh.id, extractExceptionMessage(reason)))
        return self.give_up()

    def update_history_in_progress(self, 
                            error_code = PULSE2_SUCCESS_ERROR, 
                            stdout = '',
                            stderr = ''):
        """
        Logging the MSC activity - switch the state to running.

        @param error_code: returned error code
        @type error_code: int

        @param stdout: remote command output
        @type stdout: str

        @param stderr: remote command error output
        @type stderr: str
        """
        self._update_history("running", error_code, stdout, stderr)

    def update_history_done(self, 
                            error_code = PULSE2_SUCCESS_ERROR, 
                            stdout = '',
                            stderr = ''):
        """
        Logging the MSC activity - switch the state to done.

        @param error_code: returned error code
        @type error_code: int

        @param stdout: remote command output
        @type stdout: str

        @param stderr: remote command error output
        @type stderr: str
        """
        self._update_history("done", error_code, stdout, stderr)

    def update_history_failed(self, 
                            error_code = PULSE2_SUCCESS_ERROR, 
                            stdout = '',
                            stderr = ''):
        """
        Logging the MSC activity - switch the state to failed.

        @param error_code: returned error code
        @type error_code: int

        @param stdout: remote command output
        @type stdout: str

        @param stderr: remote command error output
        @type stderr: str
        """
        self._update_history("failed", error_code, stdout, stderr)


    def _update_history(self, state, error_code, stdout, stderr):
        """
        Logging the MSC activity.

        @param error_code: returned error code
        @type error_code: int

        @param stdout: remote command output
        @type stdout: str

        @param stderr: remote command error output
        @type stderr: str
        """
        encoding = SchedulerConfig().dbencoding
        history = CommandsHistory()
        history.fk_commands_on_host = self.coh.id
        history.date = time.time()
        history.error_code = error_code
        history.stdout = stdout.encode(encoding, 'replace')
        history.stderr = stderr.encode(encoding, 'replace')
        history.phase = self.name
        history.state = state
        history.flush()

    def get_client(self, announce):
        client_group = ""

        for pref_net_ip, pref_netmask in SchedulerConfig().preferred_network :
            if NetUtils.on_same_network(self.host, pref_net_ip, pref_netmask):

                client_group = pref_net_ip
                break
                                                                                    

        return {'host': self.host, 
                'uuid': self.target.getUUID(), 
                'maxbw': self.cmd.maxbw, 
                'protocol': 'ssh', 
                'client_check': getClientCheck(self.target), 
                'server_check': getServerCheck(self.target), 
                'action': getAnnounceCheck(announce), 
                'group': client_group
               }
    def give_up(self):
        self.logger.debug("Circuit #%s: Releasing" % self.coh.id)
        return DIRECTIVE.GIVE_UP

    def failed(self):
        return DIRECTIVE.FAILED

    def switch_phase_failed(self, decrement=True):
        delay = self.calc_next_attempt_delay()
        self.coh.reSchedule(delay, decrement)
        
        ret = self.phase.switch_to_failed()
        if self.coh.is_out_of_attempts():
            logging.getLogger().info("Circuit #%s: failed" % (self.coh.id))
            self.coh.setStateFailed()
            return DIRECTIVE.KILLED
        return self.failed() 
           
    def parse_order(self, name, taken_in_account):
        if taken_in_account: # success
            self.update_history_in_progress()
            self.logger.info("Circuit #%s: %s order stacked" %
                    (self.coh.id, name))
            return self.give_up()
        else: # failed: launcher seems to have rejected it
            self.coh.setStateScheduled()
            self.logger.warn("Circuit #%s: %s order NOT stacked" % (self.coh.id, name))
            return self.switch_phase_failed(True)



import traceback

class QueryContext :
    """A simply aliasing of CoHQuery container of circuit. """

    def __init__(self, running_phase):
        self.coh = running_phase.coh
        self.cmd = running_phase.cmd
        self.phase = running_phase.phase
        self.target = running_phase.target

  

class CircuitBase(object):
    """
    A container processing the base workflow.

    
    """
    status = CC_STATUS.ACTIVE
    # Main container of selected phases
    phases = None
 
    # methods called by scheduler-proxy
    _proxy_methods = {}
    # list of phases to refer phase objects 
    installed_phases = []
    # Main container of selected phases
    _phases = None
    # msc data persistence model
    cohq = None
    # running phase reference
    running_phase = None
    # detected IP address of target
    host = None
    # first initialisation flag 
    initialized = False
    # A callable to self-releasing from the container 
    releaser = None
    # last activity timestamp
    last_activity_time = None
    # 
    launcher = None
    launchers_provider = None

    def __init__(self, id, installed_phases):
        """
        @param id: CommandOnHost id
        @type id: int

        @param installed_phases: all possible phases classes to use
        @type installed_phases: list
        """
        self.logger = logging.getLogger()
        self.id = id

        self.cohq = CoHQuery(int(id))
        self.installed_phases = installed_phases

    @property 
    def is_running(self):
        return isinstance(self.running_phase, Phase)

    def setup(self):
        """
        Post-init - detecting the networking info of target.
        """

        if not self.initialized :
            d = maybeDeferred(self._flow_create)
 
            d.addCallback(self._chooseClientNetwork)
            d.addCallback(self._host_detect) 
            d.addCallback(self._network_detect)
            d.addCallback(self._init_end)
            d.addErrback(self._init_failed)

            return d
        else :
            return Deferred()
     
    def _flow_create(self):
        """ Builds the workflow of circuit """
        
        phases = []
        selected = self.cohq.get_phases()
        for phase_name in selected :
            matches = [p for p in self.installed_phases if p.name==phase_name]
            if len(matches) == 1:
                phases.append(matches[0])
            else :
                # TODO - log it and process something .. ?
                raise KeyError


        self.phases = phases
        return True

    @property
    def phases(self):
        """Gets the phases iterator"""
        return self._phases


    @phases.setter 
    def phases(self, value):
        """
        Phases property set processing.

        - Initial verifications of list of phases
        - converting the _phases attribute to iterator
        """
        if isinstance(value, list) and all(p for p in value if issubclass(p, Phase)):
            self._phases = iter(value)
        else :
            raise TypeError("All elements must be <Phase> type")


    def install_releaser(self, releaser):
        if callable(releaser) :
            self.releaser = releaser
        else :
            raise TypeError("Releaser must be a callable")

    def install_dispatcher(self, dispatcher):
        self.dispatcher = dispatcher

        # handle the dispatcher's release() method
        self.install_releaser(dispatcher.release)

    def release(self, suspend_to_waitings=False):
        """
        A 'self-destroy' method called on end of circuit.

        Called by MscContainer which contains list of processing circuits.
        This method is called when the circuits ends.  
        """
        try :
            self.releaser(self.cohq.coh.id, suspend_to_waitings)
        except :
            self.logger.error("release error: \033[31m %s\033[0m" % traceback.format_exc())


    @property
    def qm(self):
        """
        An aliasing context to CoHQeury container

        Aliased contexts:
        - self.qm.coh
        - self.qm.cmd
        - self.qm.phase
        - self.qm.target
        """
        return QueryContext(self.running_phase)

    def _chooseClientNetwork(self, reason=None):
        """
        Choosing the correct IP address based on target info.

        @param reason: void parameter, used as twisted callback reason
        @type reason: twisted callback reason
        """
        return chooseClientNetwork(self.cohq.target)

      
    def _host_detect(self, host):
        """
        Network address detect callback.
        
        Invoked by correct IP address of machine.

        @param host: IP address
        @type host: str

        @return: network address
        @rtype: str
        """
        if not host :
            return None

        self.host = host

        for pref_net_ip, pref_netmask in SchedulerConfig().preferred_network :
            if NetUtils.on_same_network(host, pref_net_ip, pref_netmask):

                return pref_net_ip

        if len(SchedulerConfig().preferred_network) > 0 :
            self.logger.debug("Circuit #%s: network detect failed, assigned the first of scheduler" % (self.id))
            (pref_net_ip, pref_netmask) = SchedulerConfig().preferred_network[0] 
            return pref_net_ip
 

        return None

 
    def _network_detect(self, address):
        """
        Network detect callback.

        @param address: network address
        @type address: str

        @return: True and Circuit instance when success
        @rtype: tuple
        """
        if address :
            self.network_address = address
            return (True, self)
        else :
            return (False, None)

    def _init_end(self, reason):
        """
        The final callback of initialization of circuit.

        @param reason: True and Circuit instance when success
        @type reason: tuple

        @return reason: True and Circuit instance when success
        @rtype reason: tuple
        """
        if reason[0] :
            self.initialized = True
        return reason

    def _init_failed(self, failure):
        """
        Setup errorback.

        @param failure: failure reason
        @type failure: twisted failure
        """
        self.logger.error("An error occured while detecting target's ip address: %s" % str(failure))

class Circuit (CircuitBase):
 
    def run(self):
        """ Start the workflow scenario. """
        assert self.host, "host info empty"

        self.logger.debug("circuit #%s - assigned network: %s" % (self.id, self.network_address))
 
        try :
            if not self.running_phase:

                first = next(self.phases)
                self.running_phase = first(self.cohq, self.host)
                self.running_phase.launchers_provider = self.launchers_provider
                self.running_phase.dispatcher = self.dispatcher

        except StopIteration :
            self.release()
            return
 
        return self.phase_process(True) 

    def phase_process(self, result):
        """
        A callback to recursive phase processing.
        Can be called as an ordinnary routine (i.e. on start) 

        @param result: returned result from initial phase tests
        @type result: str

        @return: recursive workflow routine
        @rtype: func
        """

        # if give-up - actual phase is probably running - do not move - wait...
        if result == DIRECTIVE.GIVE_UP or result == None :
            return lambda : DIRECTIVE.GIVE_UP
        elif result == DIRECTIVE.FAILED :
            self.logger.info("Circuit #%s: failed - releasing" % self.id)
            self.release(True)
            return
        else :
            return self.phase_step() 


    def phase_error(self, failure): 
        """
        Phase processing errorback.

        @param failure: failure reason
        @type failure: twisted failure
        """
        self.logger.error("Phase error: %s" % str(failure))


    def phase_step(self): 
        """
        Main workflow processing.

        standard chain call over all the phases :
        Initial state tests resolves the next flow - perform actual phase
        or skip to the next (or wait if actual phase is running)...

        @return: recursive workflow routine
        @rtype: func
        """

        # state tests before phase processing to resolving next flow
        res = self.running_phase.apply_initial_rules()

        # move on the next phase ->
        if res == DIRECTIVE.NEXT :

            try :
                next_phase = next(self.phases)
                self.running_phase = next_phase(self.cohq, self.host)
                self.running_phase.launchers_provider = self.launchers_provider
                self.running_phase.dispatcher = self.dispatcher
                self.logger.debug("next phase :%s" % (self.running_phase))
            except StopIteration :
                # end of workflow - done !
                self.logger.info("Circuit #%d: done" % self.id)
                self.release()
            except Exception, e:
                self.logger.error("Next phase get failed: %s"  % str(e))

            else:
                d = Deferred()
                self.logger.debug("next phase: %s" % (self.running_phase))
                d.addCallback(self.phase_process)
                d.addErrback(self.phase_error)
                d.callback(True)
 
        # perform the phase (initial rules allready passed)
        elif res == DIRECTIVE.PERFORM :
            d = maybeDeferred(self.running_phase.perform)
            self.logger.debug("perform the phase: %s" % (self.running_phase))
            d.addCallback(self.phase_process)
            d.addCallback(self._last_activity_record)
            d.addErrback(self.phase_error)
            return d

        # give-up - actual phase is probably running
        elif res == DIRECTIVE.GIVE_UP :
            return False
        elif res == DIRECTIVE.OVER_TIMED :
            self.logger.info("Circuit #%s: overtimed" % self.id)
            #self.logger.info("Circuit #%s: overtimed" % self.running_phase.coh.id)
            self.running_phase.coh.setStateOverTimed()
            self.release()
            return
        elif res == DIRECTIVE.KILLED :
            #self.logger.info("Circuit #%s: killed" % self.running_phase.coh.id)
            self.logger.info("Circuit #%s: killed" % self.id)
            self.release()
            return
        else :
            self.logger.error("UNRECOGNIZED DIRECTIVE") 

    def _last_activity_record(self, reason):
        now = time.time()
        self.last_activity_time = now
        self.running_phase.last_activity_time = now

        return reason
               
    def gotErrorInResult(self, id, reason):
        self.logger.error("Circuit #%s: got an error within an result: %s" % (id, extractExceptionMessage(reason)))
        return DIRECTIVE.GIVE_UP


class MscContainer (object):
    __metaclass__ = SingletonN
    """
    Main database of circuits and access methods.

    All circuits to run are stocked here.
    """
    slots = {}
 
    # All the workflow circuits stocked here
    _circuits = []

    # A lookup to refer all phases to use
    installed_phases = []

    @property 
    def circuits(self):
        return [c for c in self._circuits if c.status == CC_STATUS.ACTIVE]
    @property 
    def waiting_circuits(self):
        return [c for c in self._circuits if c.status == CC_STATUS.WAITING]

    def remove_circuit(self, circuit):
        self._circuits.remove(circuit)

    @property
    def max_slots(self):
        return reduce(lambda x, y: (x + y), self.slots.values())

    @property
    def free_slots(self):
        return self.max_slots - len(self.get_active_circuits())

    def _in_waitings(self, id):
        """
        Test if a circuit is waiting.

        @param id: commands_on_host id
        @type id: int

        @return: True if command_on_host in container
        @rtype: bool
        """
        return id in [wf.id for wf in self.waiting_circuits]
 
    def __contains__(self, id):
        """ 
        Test if a circuit is already running,
        that means not released yet or added.

        @param id: commands_on_host id
        @type id: int

        @return: True if command_on_host in container
        @rtype: bool
        """
        return id in [wf.id for wf in self.circuits]

    def initialize(self):
        self.logger = logging.getLogger()
        self.config = SchedulerConfig()
        
        self.launchers_networks = dict([(launcher,[n[0] for n in net_and_mask]) 
                  for (launcher,net_and_mask) in self.config.launchers_networks.items()])
        self.logger.info("preferred networks by launchers: %s" % str(self.launchers_networks))
        self.launchers = self.config.launchers_uri
        # FIXME - main default launcher
        temp_launcher = self.launchers.keys()[0]
        self.launchers_provider = RemoteCallProxy(self.config.launchers_uri, temp_launcher)
        return self._get_all_slots()


    def _get_all_slots(self):
        """
        Detects the total of slots from all launchers.

        @return: total of slots per launcher
        @rtype: dict
        """
        d = self.launchers_provider.get_all_slots()

        d.addCallback(self._set_slots)
        d.addCallback(self._slots_info)
        @d.addErrback
        def _eb(failure):
            self.logger.error("An error occured when getting the slots:  %s" % failure)

        return d
     

    def _set_slots(self, slots):
        """
        Sets the detected slots from launchers

        @param slots: total of slots per launcher
        @type slots: dict
        """
        self.slots = slots
        return slots

    def _slots_info(self, result):
        """A little log stuff on start"""
        self.logger.info("Detected slots SUM from all launchers: %d" % self.max_slots)
        return result

    def has_free_slots(self):
        return len(self.get_running_circuits()) < self.max_slots
 
    def get(self, id):
        """
        Get the circuit if exists.

        @param id: commands_on_host id
        @type id: int

        @return: requested circuit 
        @rtype: Circuit object
        """
        matches = [wf for wf in self._circuits if wf.id == id]
        if len(matches) > 0 :
            return matches[0]
        else :
            self.logger.debug("Circuit #%s: not exists" % id)
            return None

    def _release(self, id, suspend_to_waitings=False):
        """
        Circuit releasing from the main container.
        
        Called typicaly when last phase ends or overtimed. 
        A reference of this method is passed on each running phase to call
        when finished or overtimed.

        @param id: commands_on_host id
        @type id: int
        """
        if id in self :
            self.logger.debug("circuit #%d finished" % id)
            circuit = self.get(id)
            if suspend_to_waitings :
                circuit.status = CC_STATUS.WAITING
                self.logger.info("Circuit #%d: failed and queued" % id)
            else :
                self.remove_circuit(circuit)
            self.logger.info("Remaining content: %d circuits (+%d waitings)" % ((len(self.circuits)),len(self.waiting_circuits)))
            return True
        
        return False

