<?

/*
 * (c) 2004-2007 Linbox / Free&ALter Soft, http://linbox.com
 * (c) 2007 Mandriva, http://www.mandriva.com
 *
 * $Id$
 *
 * This file is part of Mandriva Management Console (MMC).
 *
 * MMC is free software; you can redistribute it and/or modify
 * it under the terms of the GNU General Public License as published by
 * the Free Software Foundation; either version 2 of the License, or
 * (at your option) any later version.
 *
 * MMC is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU General Public License for more details.
 *
 * You should have received a copy of the GNU General Public License
 * along with MMC; if not, write to the Free Software
 * Foundation, Inc., 51 Franklin St, Fifth Floor, Boston, MA  02110-1301  USA
 */

require_once('modules/msc/includes/widgets.inc.php');
require_once('modules/msc/includes/utilities.php');
require_once('modules/msc/includes/qactions.inc.php');
require_once('modules/msc/includes/mirror_api.php');
require_once('modules/msc/includes/commands_xmlrpc.inc.php');
require_once('modules/msc/includes/package_api.php');
require_once('modules/msc/includes/scheduler_xmlrpc.php');
require_once('modules/msc/includes/mscoptions_xmlrpc.php');

function quick_get($param, $is_checkbox = False) {
    if ($is_checkbox) { return $_GET[$param]; }
    if (isset($_POST[$param]) && $_POST[$param] != '') { return $_POST[$param]; }
    return $_GET[$param];
}

function start_a_command($proxy = array()) {
    $error = "";
    if (!check_date($_POST)) {
        $error .= _T("Your start and end dates are not coherant, please check them.<br/>", "msc");
    }
    # should add some other tests on fields (like int are int? ...)
    if ($error != '') {
        new NotifyWidgetFailure($error);
        complete_post();
        $url = "base/computers/msctabs?";
        foreach ($_GET as $k => $v) {
            $url .= "$v=$k";
        }
        header("Location: " . urlStrRedirect("base/computers/msctabs", array_merge($_GET, $_POST, array('failure'=>True))));
        return False;
    }
    // Vars seeding
    $post = $_POST;
    $from = $post['from'];
    $path =  explode('|', $from);
    $module = $path[0];
    $submod = $path[1];
    $page = $path[2];
    $params = array();
    foreach (array('start_script', 'clean_on_success', 'do_reboot', 'do_wol', 'next_connection_delay','max_connection_attempt', 'do_inventory', 'ltitle', 'parameters', 'papi', 'maxbw', 'deployment_intervals', 'max_clients_per_proxy') as $param) {
        $params[$param] = $post[$param];
    }
    $halt_to = array();
    foreach ($post as $p=>$v) {
        if (preg_match('/^issue_halt_to_/', $p)) {
            $p = preg_replace('/^issue_halt_to_/', '', $p);
            $halt_to[] = $p;
        }
    }
    $params['issue_halt_to'] = $halt_to;
    $p_api = new ServerAPI();
    $p_api->fromURI($post['papi']);

    foreach (array('start_date', 'end_date') as $param) {
        if ($post[$param] == _T("now", "msc")) {
            $params[$param] = "0000-00-00 00:00:00";
        } elseif ($post[$param] == _T("never", "msc")) {
            $params[$param] = "0000-00-00 00:00:00";
        } else
            $params[$param] = $post[$param];
    }

    $pid = $post['pid'];
    $mode = $post['copy_mode'];

    if (isset($post['uuid']) && $post['uuid']) { // command on a single target
        $hostname = $post['hostname'];
        $uuid = $post['uuid'];
        $target = array($uuid);
        $tab = 'tablogs';
        /* record new command */
        $id = add_command_api($pid, $target, $params, $p_api, $mode, NULL);
        if (!isXMLRPCError()) {
            scheduler_start_these_commands('', array($id));
            /* then redirect to the logs page */
            header("Location: " . urlStrRedirect("$module/$submod/$page", array('tab'=>$tab, 'uuid'=>$uuid, 'hostname'=>$hostname, 'cmd_id'=>$id)));
        } else {
            /* Return to the launch tab, the backtrace will be displayed */
            header("Location: " . urlStrRedirect("$module/$submod/$page", array('tab'=>'tablaunch', 'uuid'=>$uuid, 'hostname'=>$hostname)));
        }
    } else { # command on a whole group
        $gid = $post['gid'];
        $tab = 'grouptablogs';
        // record new command
        // given a proxy list and a proxy style, we now have to build or proxy chain
        // target structure is an dict using the following stucture: "priority" => array(proxies)

        $ordered_proxies = array();
        if ($_POST['proxy_mode'] == 'multiple') { // first case: split mode; every proxy got the same priority (1 in our case)
            foreach ($proxy as $p) {
                array_push($ordered_proxies, array('uuid' => $p, 'priority' => 1, 'max_clients' => $_POST['max_clients_per_proxy']));
            }
            $params['proxy_mode'] = 'split';
        } elseif ($_POST['proxy_mode'] == 'single') { // second case: queue mode; one priority level per proxy, starting at 1
            $current_priority = 1;
            foreach ($proxy as $p) {
                array_push($ordered_proxies, array('uuid' => $p, 'priority' => $current_priority, 'max_clients' => $_POST['max_clients_per_proxy']));
                $current_priority += 1;
            }
            $params['proxy_mode'] = 'queue';
        }

        $id = add_command_api($pid, NULL, $params, $p_api, $mode, $gid, $ordered_proxies);
        scheduler_start_these_commands('', array($id));
        // then redirect to the logs page
        header("Location: " . urlStrRedirect("$module/$submod/$page", array('tab'=>$tab, 'gid'=>$gid, 'cmd_id'=>$id, 'proxy' => $proxy)));
    }
}
function complete_post() {
    foreach (array( 'start_script', 'clean_on_success', 'do_wol', 'do_inventory', 'issue_halt_to_done') as $mandatory) {
        if (!isset($_POST[$mandatory])) {
            $_POST[$mandatory] = '';
        }
    }
}
function check_date($post) {
    $start = "0000-00-00 00:00:00";
    $end = "0000-00-00 00:00:00";
    $now = getdate();
    $now = $now['year'].'-'.$now['mon'].'-'.$now['mday'].' '.$now['hours'].':'.$now['minutes'].':'.$now['seconds'];
    if ($post['start_date'] != _T("now", "msc") && $post['start_date'] != "") {
        $start = $post['start_date'];
    }
    if ($post['end_date'] != _T("never", "msc") && $post['end_date'] != "") {
        $end = $post['end_date'];
    }
    if ($end == "0000-00-00 00:00:00") { # never end
        return True;
    }
    if ($start == "0000-00-00 00:00:00" and $end != "0000-00-00 00:00:00") {
        if (!check_for_real($now, $end)) { return False; } # start now, but finish in the past
    }
    return check_for_real($start, $end);
}
function check_for_real($s, $e) {
    $start = preg_split("/[ :-]/", $s);
    $end   = preg_split("/[ :-]/", $e);
    
    for ($i = 0; $i < 6; $i++) {
        if ($start[$i] > $end[$i]) {
            return False;
        } else if ($start[$i] < $end[$i]) {
            return True;
        }
    }
    return False;
}


/* Validation on local proxies selection page */
if (isset($_POST["bconfirmproxy"])) {
    $proxy = array();
    if (isset($_POST["lpmembers"])) {
        if ($_POST["local_proxy_selection_mode"] == "semi_auto") {
            $members = unserialize(base64_decode($_POST["lpmachines"]));
            foreach($members as $member => $name) {
                $computer = preg_split("/##/", $member);
                $proxy[] = $computer[1];
            }

            shuffle($proxy);
            $proxy = array_splice($proxy, 0, $_POST['proxy_number']);
        } elseif ($_POST["local_proxy_selection_mode"] == "manual") {
            $members = unserialize(base64_decode($_POST["lpmembers"]));
            foreach($members as $member => $name) {
                $computer = preg_split("/##/", $member);
                $proxy[] = $computer[1];
            }
        }
    }
    start_a_command($proxy);
}


/* cancel button handling */
if (isset($_POST['bback'])) {
    $from = $_POST['from'];
    $path =  explode('|', $from);
    $module = $path[0];
    $submod = $path[1];
    $page = $path[2];
    if (isset($_POST["gid"]))
        header("Location: " . urlStrRedirect("$module/$submod/$page", array('tab'=>"grouptablaunch", 'gid'=>$_POST["gid"])));
    if (isset($_POST["uuid"]))
        header("Location: " . urlStrRedirect("$module/$submod/$page", array('tab'=>"msctabs", 'uuid'=>$_POST["uuid"])));
}

/* local proxy selection handling */
if (isset($_POST['local_proxy']))
    require('modules/msc/msc/local_proxy.php');

/* Advanced Action Post Handling */
if (isset($_GET['badvanced']) and isset($_POST['bconfirm']) and !isset($_POST['local_proxy'])) {
    start_a_command();
}

/* Advanced action: form display */
if (isset($_GET['badvanced']) and !isset($_POST['bconfirm'])) {
    // Vars seeding
    $from = quick_get('from');
    $pid = quick_get('pid');
    $p_api = new ServerAPI();
    $p_api->fromURI(quick_get("papi"));
    $name = quick_get('ltitle');
    if (!isset($name) || $name == '') {
        $name = getPackageLabel($p_api, quick_get('pid'));
    }

    // form design
    $f = new Form();

    // display top label
    if (isset($_GET['uuid']) && $_GET['uuid']) {
        $hostname = $_GET['hostname'];
        $uuid = $_GET['uuid'];
        $machine = getMachine(array('uuid'=>$uuid), True);

        $hostname = $machine->hostname;
        $label = new RenderedLabel(3, sprintf(_T('Single advanced launch : action "%s" on "%s"', 'msc'), $name, $machine->hostname));

        $f->push(new Table());
        $f->add(new HiddenTpl("uuid"),  array("value" => $uuid,         "hide" => True));
        $f->add(new HiddenTpl("name"),  array("value" => $hostname,     "hide" => True));
    } else {
        $gid = $_GET['gid'];
        $group = new Group($gid, true);
        if ($group->exists != False) {
            $label = new RenderedLabel(3, sprintf(_T('Group Advanced launch : action "%s" on "%s"', 'msc'), $name, $group->getName()));
            $f->push(new Table());
            $f->add(new HiddenTpl("gid"),   array("value" => $gid,          "hide" => True));
        }
    }
    $label->display();

    $f->add(new HiddenTpl("pid"),   array("value" => $pid,          "hide" => True));
    $f->add(new HiddenTpl("papi"),  array("value" => quick_get("papi"), "hide" => True));
    $f->add(new HiddenTpl("from"),  array("value" => $from,         "hide" => True));
    $f->add(new TrFormElement(_T('Command name', 'msc'),                                new InputTpl('ltitle')), array("value" => $name));
    $f->add(new TrFormElement(_T('Script parameters', 'msc'),                           new InputTpl('parameters')), array("value" => quick_get('parameters')));
    $f->add(new TrFormElement(_T('Start "Wake On Lan" query if connection fails', 'msc'), new CheckboxTpl("do_wol")), array("value" => (quick_get('do_wol', True) == 'on' ? 'checked' : '')));
    
    
    $start_script = 'on';
    $clean_on_success = 'on';
    if (quick_get('failure') == '1') {
        $start_script = quick_get('start_script', True);
        $clean_on_success = quick_get('clean_on_success', True);
    }
    $f->add(new TrFormElement(_T('Start script', 'msc'),                                new CheckboxTpl("start_script")), array("value" => $start_script == 'on' ? 'checked' : ''));
    $f->add(new TrFormElement(_T('Delete files after a successful execution', 'msc'),   new CheckboxTpl("clean_on_success")), array("value" => $clean_on_success == 'on' ? 'checked' : ''));
    $f->add(new TrFormElement(_T('Do an inventory after a successful execution', 'msc'),new CheckboxTpl("do_inventory")), array("value" => quick_get('do_inventory', True) == 'on' ? 'checked' : ''));
    $f->add(new TrFormElement(_T('Halt client after', 'msc'), new CheckboxTpl("issue_halt_to_done", _T("done", "msc"))), array("value" => quick_get('issue_halt_to_done', True) == 'on' ? 'checked' : ''));
    /*$f->add(new TrFormElement('', new CheckboxTpl("issue_halt_to_failed", _T("failed", "msc"))), array("value" => $_GET['issue_halt_to_failed'] == 'on' ? 'checked' : ''));
    $f->add(new TrFormElement('', new CheckboxTpl("issue_halt_to_over_time", _T("over time", "msc"))), array("value" => $_GET['issue_halt_to_over_time'] == 'on' ? 'checked' : ''));
    $f->add(new TrFormElement('', new CheckboxTpl("issue_halt_to_out_of_interval", _T("out of interval", "msc"))), array("value" => $_GET['issue_halt_to_out_of_interval'] == 'on' ? 'checked' : ''));*/

    $f->add(new TrFormElement(_T('Maximum number of connection attempt', 'msc'),        new InputTpl("max_connection_attempt")), array("value" => quick_get('max_connection_attempt')));
    $f->add(new TrFormElement(_T('Delay between two connections (minutes)', 'msc'),     new InputTpl("next_connection_delay")), array("value" => quick_get('next_connection_delay')));
    $f->add(new TrFormElement(_T('The command may start after', 'msc'),                 new DynamicDateTpl('start_date')), array('ask_for_now' => 1, "value"=>quick_get('start_date')));
    $f->add(new TrFormElement(_T('The command must stop before', 'msc'),                new DynamicDateTpl('end_date')), array('ask_for_never' => 1, "value"=>quick_get('end_date')));
    $f->add(new TrFormElement(_T('Deployment interval', 'msc'),                         new InputTpl('deployment_intervals')), array("value" => quick_get('deployment_intervals')));
    $max_bw = quick_get('maxbw');
    if (!isset($max_bw) || $max_bw == '') { $max_bw = web_def_maxbw(); }
    $f->add(new TrFormElement(_T('Max bandwidth (b/s)', 'msc'),                         new NumericInputTpl('maxbw')), array("value" => $max_bw));
    if (web_force_mode()) {
        $f->add(new HiddenTpl("copy_mode"),         array("value" => web_def_mode(), "hide" => True));
    } else {
        $rb = new RadioTpl("copy_mode");
        $rb->setChoices(array(_T('push', 'msc'), _T('push / pull', 'msc')));
        $rb->setvalues(array('push', 'push_pull'));
        $rb->setSelected($_GET['copy_mode']);
        $f->add(new TrFormElement(_T('Copy Mode', 'msc'), $rb));
    }

    /* Only display local proxy button on a group and if allowed */
    if (isset($_GET['gid']) && strlen($_GET['gid']) && web_allow_local_proxy()) {
        $f->add(new TrFormElement(_T('Deploy using a local proxy', 'msc'),
                                  new CheckboxTpl("local_proxy")), array("value" => ''));
    }

    $f->pop();
    $f->addValidateButton("bconfirm");
    $f->addCancelButton("bback");
    $f->display();

}
### /Advanced actions handling ###

/* single target: form display */
if (!isset($_GET['badvanced']) && $_GET['uuid'] && !isset($_POST['launchAction'])) {
    $machine = new Machine();
    $machine->uuid = $_GET['uuid'];
    $machine->hostname = $_GET['hostname'];
    $msc_host = new RenderedMSCHost($machine);
    $msc_host->ajaxDisplay();
    
    $msc_actions = new RenderedMSCActions(msc_script_list_file(), $machine->hostname, array('uuid'=>$_GET['uuid']));
    $msc_actions->display();
    
    $ajax = new AjaxFilter("modules/msc/msc/ajaxPackageFilter.php?uuid=".$machine->uuid."&hostname=".$machine->hostname);
    $ajax->display();
    $ajax->displayDivToUpdate();

}

/* group display */
if (!isset($_GET['badvanced']) && isset($_GET['gid']) && !isset($_POST['launchAction']) && !isset($_GET['uuid'])) {
    $group = new Group($_GET['gid'], true);
    if ($group->exists != False) {
        // Display the actions list
        $msc_actions = new RenderedMSCActions(msc_script_list_file(), $group->getName(), array("gid"=>$_GET['gid']));
        $msc_actions->display();

        $ajax = new AjaxFilter("modules/msc/msc/ajaxPackageFilter.php", "container", array("gid"=>$_GET['gid']));
        $ajax->display();
        print "<br/>";
        $ajax->displayDivToUpdate();
    } else {
        $msc_host = new RenderedMSCGroupDontExists();
        $msc_host->headerDisplay();
    }
}


?>
<style>
.primary_list { }
.secondary_list {
    background-color: #e1e5e6 !important;
}

</style>
