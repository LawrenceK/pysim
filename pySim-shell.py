#!/usr/bin/env python3

# Interactive shell for working with SIM / UICC / USIM / ISIM cards
#
# (C) 2021-2022 by Harald Welte <laforge@osmocom.org>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 2 of the License, or
# (at your option) any later version.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

from typing import List

import json
import traceback

import cmd2
from packaging import version
from cmd2 import style
# cmd2 >= 2.3.0 has deprecated the bg/fg in favor of Bg/Fg :(
if version.parse(cmd2.__version__) < version.parse("2.3.0"):
    from cmd2 import fg, bg # pylint: disable=no-name-in-module
    RED = fg.red
    LIGHT_RED = fg.bright_red
    LIGHT_GREEN = fg.bright_green
else:
    from cmd2 import Fg, Bg # pylint: disable=no-name-in-module
    RED = Fg.RED
    LIGHT_RED = Fg.LIGHT_RED
    LIGHT_GREEN = Fg.LIGHT_GREEN
from cmd2 import CommandSet, with_default_category, with_argparser
import argparse

import os
import sys
from pathlib import Path
from io import StringIO

from pprint import pprint as pp

from pySim.exceptions import *
from pySim.commands import SimCardCommands
from pySim.transport import init_reader, ApduTracer, argparse_add_reader_args, ProactiveHandler
from pySim.cards import card_detect, SimCard
from pySim.utils import h2b, swap_nibbles, rpad, b2h, JsonEncoder, bertlv_parse_one, sw_match
from pySim.utils import sanitize_pin_adm, tabulate_str_list, boxed_heading_str, Hexstr
from pySim.card_handler import CardHandler, CardHandlerAuto

from pySim.filesystem import RuntimeState, CardDF, CardADF, CardModel
from pySim.profile import CardProfile
from pySim.cdma_ruim import CardProfileRUIM
from pySim.ts_102_221 import CardProfileUICC
from pySim.ts_102_222 import Ts102222Commands
from pySim.ts_31_102 import CardApplicationUSIM
from pySim.ts_31_103 import CardApplicationISIM
from pySim.ts_31_104 import CardApplicationHPSIM
from pySim.ara_m import CardApplicationARAM
from pySim.global_platform import CardApplicationISD
from pySim.gsm_r import DF_EIRENE
from pySim.cat import ProactiveCommand

# we need to import this module so that the SysmocomSJA2 sub-class of
# CardModel is created, which will add the ATR-based matching and
# calling of SysmocomSJA2.add_files.  See  CardModel.apply_matching_models
import pySim.sysmocom_sja2

from pySim.card_key_provider import CardKeyProviderCsv, card_key_provider_register, card_key_provider_get_field


def init_card(sl):
    """
    Detect card in reader and setup card profile and runtime state. This
    function must be called at least once on startup. The card and runtime
    state object (rs) is required for all pySim-shell commands.
    """

    # Wait up to three seconds for a card in reader and try to detect
    # the card type.
    print("Waiting for card...")
    try:
        sl.wait_for_card(3)
    except NoCardError:
        print("No card detected!")
        return None, None
    except:
        print("Card not readable!")
        return None, None

    generic_card = False
    card = card_detect("auto", scc)
    if card is None:
        print("Warning: Could not detect card type - assuming a generic card type...")
        card = SimCard(scc)
        generic_card = True

    profile = CardProfile.pick(scc)
    if profile is None:
        print("Unsupported card type!")
        return None, card

    # ETSI TS 102 221, Table 9.3 specifies a default for the PIN key
    # references, however card manufactures may still decide to pick an
    # arbitrary key reference. In case we run on a generic card class that is
    # detected as an UICC, we will pick the key reference that is officially
    # specified.
    if generic_card and isinstance(profile, CardProfileUICC):
        card._adm_chv_num = 0x0A

    print("Info: Card is of type: %s" % str(profile))

    # FIXME: This shouln't be here, the profile should add the applications,
    # however, we cannot simply put his into ts_102_221.py since we would
    # have to e.g. import CardApplicationUSIM from ts_31_102.py, which already
    # imports from ts_102_221.py. This means we will end up with a circular
    # import, which needs to be resolved first.
    if isinstance(profile, CardProfileUICC):
        profile.add_application(CardApplicationUSIM())
        profile.add_application(CardApplicationISIM())
        profile.add_application(CardApplicationHPSIM())
        profile.add_application(CardApplicationARAM())
        profile.add_application(CardApplicationISD())

    # Create runtime state with card profile
    rs = RuntimeState(card, profile)

    # FIXME: This is an GSM-R related file, it needs to be added throughout,
    # the profile. At the moment we add it for all cards, this won't hurt,
    # but regular SIM and UICC will not have it and fail to select it.
    rs.mf.add_file(DF_EIRENE())

    CardModel.apply_matching_models(scc, rs)

    # inform the transport that we can do context-specific SW interpretation
    sl.set_sw_interpreter(rs)

    return rs, card


class PysimApp(cmd2.Cmd):
    CUSTOM_CATEGORY = 'pySim Commands'

    def __init__(self, card, rs, sl, ch, script=None):
        if version.parse(cmd2.__version__) < version.parse("2.0.0"):
            kwargs = {'use_ipython': True}
        else:
            kwargs = {'include_ipy': True}

        # pylint: disable=unexpected-keyword-arg
        super().__init__(persistent_history_file='~/.pysim_shell_history', allow_cli_args=False,
                         auto_load_commands=False, startup_script=script, **kwargs)
        self.intro = style('Welcome to pySim-shell!', fg=RED)
        self.default_category = 'pySim-shell built-in commands'
        self.card = None
        self.rs = None
        self.lchan = None
        self.py_locals = {'card': self.card, 'rs': self.rs, 'lchan': self.lchan}
        self.sl = sl
        self.ch = ch

        self.numeric_path = False
        self.conserve_write = True
        self.json_pretty_print = True
        self.apdu_trace = False

        if version.parse(cmd2.__version__) < version.parse("2.0.0"):
            # pylint: disable=no-value-for-parameter
            self.add_settable(cmd2.Settable('numeric_path', bool, 'Print File IDs instead of names',
                                            onchange_cb=self._onchange_numeric_path))
            # pylint: disable=no-value-for-parameter
            self.add_settable(cmd2.Settable('conserve_write', bool, 'Read and compare before write',
                                            onchange_cb=self._onchange_conserve_write))
            # pylint: disable=no-value-for-parameter
            self.add_settable(cmd2.Settable('json_pretty_print', bool, 'Pretty-Print JSON output'))
            # pylint: disable=no-value-for-parameter
            self.add_settable(cmd2.Settable('apdu_trace', bool, 'Trace and display APDUs exchanged with card',
                                            onchange_cb=self._onchange_apdu_trace))
        else:
            self.add_settable(cmd2.Settable('numeric_path', bool, 'Print File IDs instead of names', self, \
                                            onchange_cb=self._onchange_numeric_path)) # pylint: disable=too-many-function-args
            self.add_settable(cmd2.Settable('conserve_write', bool, 'Read and compare before write', self, \
                                            onchange_cb=self._onchange_conserve_write)) # pylint: disable=too-many-function-args
            self.add_settable(cmd2.Settable('json_pretty_print', bool, 'Pretty-Print JSON output', self)) # pylint: disable=too-many-function-args
            self.add_settable(cmd2.Settable('apdu_trace', bool, 'Trace and display APDUs exchanged with card', self, \
                                            onchange_cb=self._onchange_apdu_trace)) # pylint: disable=too-many-function-args
        self.equip(card, rs)

    def equip(self, card, rs):
        """
        Equip pySim-shell with the supplied card and runtime state, add (or remove) all required settables and
        and commands to enable card operations.
        """

        rc = False

        # Unequip everything from pySim-shell that would not work in unequipped state
        if self.rs:
            lchan = self.rs.lchan[0]
            lchan.unregister_cmds(self)
        for cmds in [Iso7816Commands, Ts102222Commands, PySimCommands]:
            cmd_set = self.find_commandsets(cmds)
            if cmd_set:
                self.unregister_command_set(cmd_set[0])

        self.card = card
        self.rs = rs

        # When a card object and a runtime state is present, (re)equip pySim-shell with everything that is
        # needed to operate on cards.
        if self.card and self.rs:
            self.lchan = self.rs.lchan[0]
            self._onchange_conserve_write(
                'conserve_write', False, self.conserve_write)
            self._onchange_apdu_trace('apdu_trace', False, self.apdu_trace)
            if self.rs.profile:
                for cmd_set in self.rs.profile.shell_cmdsets:
                    self.register_command_set(cmd_set)
            self.register_command_set(Iso7816Commands())
            self.register_command_set(Ts102222Commands())
            self.register_command_set(PySimCommands())
            self.iccid, sw = self.card.read_iccid()
            self.lchan.select('MF', self)
            rc = True
        else:
            self.poutput("pySim-shell not equipped!")

        self.update_prompt()
        return rc

    def poutput_json(self, data, force_no_pretty=False):
        """like cmd2.poutput() but for a JSON serializable dict."""
        if force_no_pretty or self.json_pretty_print == False:
            output = json.dumps(data, cls=JsonEncoder)
        else:
            output = json.dumps(data, cls=JsonEncoder, indent=4)
        self.poutput(output)

    def _onchange_numeric_path(self, param_name, old, new):
        self.update_prompt()

    def _onchange_conserve_write(self, param_name, old, new):
        if self.rs:
            self.rs.conserve_write = new

    def _onchange_apdu_trace(self, param_name, old, new):
        if self.card:
            if new == True:
                self.card._scc._tp.apdu_tracer = self.Cmd2ApduTracer(self)
            else:
                self.card._scc._tp.apdu_tracer = None

    class Cmd2ApduTracer(ApduTracer):
        def __init__(self, cmd2_app):
            self.cmd2 = app

        def trace_response(self, cmd, sw, resp):
            self.cmd2.poutput("-> %s %s" % (cmd[:10], cmd[10:]))
            self.cmd2.poutput("<- %s: %s" % (sw, resp))

    def update_prompt(self):
        if self.lchan:
            path_str = self.lchan.selected_file.fully_qualified_path_str(not self.numeric_path)
            self.prompt = 'pySIM-shell (%s)> ' % (path_str)
        else:
            if self.card:
                self.prompt = 'pySIM-shell (no card profile)> '
            else:
                self.prompt = 'pySIM-shell (no card)> '

    @cmd2.with_category(CUSTOM_CATEGORY)
    def do_intro(self, _):
        """Display the intro banner"""
        self.poutput(self.intro)

    def do_eof(self, _: argparse.Namespace) -> bool:
        self.poutput("")
        return self.do_quit('')

    @cmd2.with_category(CUSTOM_CATEGORY)
    def do_equip(self, opts):
        """Equip pySim-shell with card"""
        if self.rs.profile:
            for cmd_set in self.rs.profile.shell_cmdsets:
                self.unregister_command_set(cmd_set)
        rs, card = init_card(sl)
        self.equip(card, rs)

    apdu_cmd_parser = argparse.ArgumentParser()
    apdu_cmd_parser.add_argument('APDU', type=str, help='APDU as hex string')
    apdu_cmd_parser.add_argument('--expect-sw', help='expect a specified status word', type=str, default=None)

    @cmd2.with_argparser(apdu_cmd_parser)
    def do_apdu(self, opts):
        """Send a raw APDU to the card, and print SW + Response.
        DANGEROUS: pySim-shell will not know any card state changes, and
        not continue to work as expected if you e.g. select a different
        file."""
        data, sw = self.card._scc._tp.send_apdu(opts.APDU)
        if data:
            self.poutput("SW: %s, RESP: %s" % (sw, data))
        else:
            self.poutput("SW: %s" % sw)
        if opts.expect_sw:
            if not sw_match(sw, opts.expect_sw):
                raise SwMatchError(sw, opts.expect_sw)

    class InterceptStderr(list):
        def __init__(self):
            self._stderr_backup = sys.stderr

        def __enter__(self):
            self._stringio_stderr = StringIO()
            sys.stderr = self._stringio_stderr
            return self

        def __exit__(self, *args):
            self.stderr = self._stringio_stderr.getvalue().strip()
            del self._stringio_stderr
            sys.stderr = self._stderr_backup

    def _show_failure_sign(self):
        self.poutput(style("  +-------------+", fg=LIGHT_RED))
        self.poutput(style("  +   ##   ##   +", fg=LIGHT_RED))
        self.poutput(style("  +    ## ##    +", fg=LIGHT_RED))
        self.poutput(style("  +     ###     +", fg=LIGHT_RED))
        self.poutput(style("  +    ## ##    +", fg=LIGHT_RED))
        self.poutput(style("  +   ##   ##   +", fg=LIGHT_RED))
        self.poutput(style("  +-------------+", fg=LIGHT_RED))
        self.poutput("")

    def _show_success_sign(self):
        self.poutput(style("  +-------------+", fg=LIGHT_GREEN))
        self.poutput(style("  +          ## +", fg=LIGHT_GREEN))
        self.poutput(style("  +         ##  +", fg=LIGHT_GREEN))
        self.poutput(style("  +  #    ##    +", fg=LIGHT_GREEN))
        self.poutput(style("  +   ## #      +", fg=LIGHT_GREEN))
        self.poutput(style("  +    ##       +", fg=LIGHT_GREEN))
        self.poutput(style("  +-------------+", fg=LIGHT_GREEN))
        self.poutput("")

    def _process_card(self, first, script_path):

        # Early phase of card initialzation (this part may fail with an exception)
        try:
            rs, card = init_card(self.sl)
            rc = self.equip(card, rs)
        except:
            self.poutput("")
            self.poutput("Card initialization failed with an exception:")
            self.poutput("---------------------8<---------------------")
            traceback.print_exc()
            self.poutput("---------------------8<---------------------")
            self.poutput("")
            return -1

        # Actual card processing step. This part should never fail with an exception since the cmd2
        # do_run_script method will catch any exception that might occur during script execution.
        if rc:
            self.poutput("")
            self.poutput("Transcript stdout:")
            self.poutput("---------------------8<---------------------")
            with self.InterceptStderr() as logged:
                self.do_run_script(script_path)
            self.poutput("---------------------8<---------------------")

            self.poutput("")
            self.poutput("Transcript stderr:")
            if logged.stderr:
                self.poutput("---------------------8<---------------------")
                self.poutput(logged.stderr)
                self.poutput("---------------------8<---------------------")
            else:
                self.poutput("(none)")

            # Check for exceptions
            self.poutput("")
            if "EXCEPTION of type" not in logged.stderr:
                return 0

        return -1

    bulk_script_parser = argparse.ArgumentParser()
    bulk_script_parser.add_argument(
        'script_path', help="path to the script file")
    bulk_script_parser.add_argument('--halt_on_error', help='stop card handling if an exeption occurs',
                                    action='store_true')
    bulk_script_parser.add_argument('--tries', type=int, default=2,
                                    help='how many tries before trying the next card')
    bulk_script_parser.add_argument('--on_stop_action', type=str, default=None,
                                    help='commandline to execute when card handling has stopped')
    bulk_script_parser.add_argument('--pre_card_action', type=str, default=None,
                                    help='commandline to execute before actually talking to the card')

    @cmd2.with_argparser(bulk_script_parser)
    @cmd2.with_category(CUSTOM_CATEGORY)
    def do_bulk_script(self, opts):
        """Run script on multiple cards (bulk provisioning)"""

        # Make sure that the script file exists and that it is readable.
        if not os.access(opts.script_path, os.R_OK):
            self.poutput("Invalid script file!")
            return

        success_count = 0
        fail_count = 0

        first = True
        while 1:
            # TODO: Count consecutive failures, if more than N consecutive failures occur, then stop.
            # The ratinale is: There may be a problem with the device, we do want to prevent that
            # all remaining cards are fired to the error bin. This is only relevant for situations
            # with large stacks, probably we do not need this feature right now.

            try:
                # In case of failure, try multiple times.
                for i in range(opts.tries):
                    # fetch card into reader bay
                    ch.get(first)

                    # if necessary execute an action before we start processing the card
                    if(opts.pre_card_action):
                        os.system(opts.pre_card_action)

                    # process the card
                    rc = self._process_card(first, opts.script_path)
                    if rc == 0:
                        success_count = success_count + 1
                        self._show_success_sign()
                        self.poutput("Statistics: success :%i, failure: %i" % (
                            success_count, fail_count))
                        break
                    else:
                        fail_count = fail_count + 1
                        self._show_failure_sign()
                        self.poutput("Statistics: success :%i, failure: %i" % (
                            success_count, fail_count))

                # Depending on success or failure, the card goes either in the "error" bin or in the
                # "done" bin.
                if rc < 0:
                    ch.error()
                else:
                    ch.done()

                # In most cases it is possible to proceed with the next card, but the
                # user may decide to halt immediately when an error occurs
                if opts.halt_on_error and rc < 0:
                    return

            except (KeyboardInterrupt):
                self.poutput("")
                self.poutput("Terminated by user!")
                return
            except (SystemExit):
                # When all cards are processed the card handler device will throw a SystemExit
                # exception. Also Errors that are not recoverable (cards stuck etc.) will end up here.
                # The user has the option to execute some action to make aware that the card handler
                # needs service.
                if(opts.on_stop_action):
                    os.system(opts.on_stop_action)
                return
            except:
                self.poutput("")
                self.poutput("Card handling failed with an exception:")
                self.poutput("---------------------8<---------------------")
                traceback.print_exc()
                self.poutput("---------------------8<---------------------")
                self.poutput("")
                fail_count = fail_count + 1
                self._show_failure_sign()
                self.poutput("Statistics: success :%i, failure: %i" %
                             (success_count, fail_count))

            first = False

    echo_parser = argparse.ArgumentParser()
    echo_parser.add_argument('string', help="string to echo on the shell")

    @cmd2.with_argparser(echo_parser)
    @cmd2.with_category(CUSTOM_CATEGORY)
    def do_echo(self, opts):
        """Echo (print) a string on the console"""
        self.poutput(opts.string)

    @cmd2.with_category(CUSTOM_CATEGORY)
    def do_version(self, opts):
        """Print the pySim software version."""
        import pkg_resources
        self.poutput(pkg_resources.get_distribution('pySim'))

@with_default_category('pySim Commands')
class PySimCommands(CommandSet):
    def __init__(self):
        super().__init__()

    dir_parser = argparse.ArgumentParser()
    dir_parser.add_argument(
        '--fids', help='Show file identifiers', action='store_true')
    dir_parser.add_argument(
        '--names', help='Show file names', action='store_true')
    dir_parser.add_argument(
        '--apps', help='Show applications', action='store_true')
    dir_parser.add_argument(
        '--all', help='Show all selectable identifiers and names', action='store_true')

    @cmd2.with_argparser(dir_parser)
    def do_dir(self, opts):
        """Show a listing of files available in currently selected DF or MF"""
        if opts.all:
            flags = []
        elif opts.fids or opts.names or opts.apps:
            flags = ['PARENT', 'SELF']
            if opts.fids:
                flags += ['FIDS', 'AIDS']
            if opts.names:
                flags += ['FNAMES', 'ANAMES']
            if opts.apps:
                flags += ['ANAMES', 'AIDS']
        else:
            flags = ['PARENT', 'SELF', 'FNAMES', 'ANAMES']
        selectables = list(
            self._cmd.lchan.selected_file.get_selectable_names(flags=flags))
        directory_str = tabulate_str_list(
            selectables, width=79, hspace=2, lspace=1, align_left=True)
        path = self._cmd.lchan.selected_file.fully_qualified_path_str(True)
        self._cmd.poutput(path)
        path = self._cmd.lchan.selected_file.fully_qualified_path_str(False)
        self._cmd.poutput(path)
        self._cmd.poutput(directory_str)
        self._cmd.poutput("%d files" % len(selectables))

    def walk(self, indent=0, action_ef=None, action_df=None, context=None, **kwargs):
        """Recursively walk through the file system, starting at the currently selected DF"""

        if isinstance(self._cmd.lchan.selected_file, CardDF):
            if action_df:
                action_df(context, opts)

        files = self._cmd.lchan.selected_file.get_selectables(
            flags=['FNAMES', 'ANAMES'])
        for f in files:
            # special case: When no action is performed, just output a directory
            if not action_ef and not action_df:
                output_str = "  " * indent + str(f) + (" " * 250)
                output_str = output_str[0:25]
                if isinstance(files[f], CardADF):
                    output_str += " " + str(files[f].aid)
                else:
                    output_str += " " + str(files[f].fid)
                output_str += " " + str(files[f].desc)
                self._cmd.poutput(output_str)

            if isinstance(files[f], CardDF):
                skip_df = False
                try:
                    fcp_dec = self._cmd.lchan.select(f, self._cmd)
                except Exception as e:
                    skip_df = True
                    df = self._cmd.lchan.selected_file
                    df_path = df.fully_qualified_path_str(True)
                    df_skip_reason_str = df_path + \
                        "/" + str(f) + ", " + str(e)
                    if context:
                        context['DF_SKIP'] += 1
                        context['DF_SKIP_REASON'].append(df_skip_reason_str)

                # If the DF was skipped, we never have entered the directory
                # below, so we must not move up.
                if skip_df == False:
                    self.walk(indent + 1, action_ef, action_df, context, **kwargs)
                    fcp_dec = self._cmd.lchan.select("..", self._cmd)

            elif action_ef:
                df_before_action = self._cmd.lchan.selected_file
                action_ef(f, context, **kwargs)
                # When walking through the file system tree the action must not
                # always restore the currently selected file to the file that
                # was selected before executing the action() callback.
                if df_before_action != self._cmd.lchan.selected_file:
                    raise RuntimeError("inconsistent walk, %s is currently selected but expecting %s to be selected"
                                       % (str(self._cmd.lchan.selected_file), str(df_before_action)))

    def do_tree(self, opts):
        """Display a filesystem-tree with all selectable files"""
        self.walk()

    def export_ef(self, filename, context, as_json):
        """ Select and export a single elementary file (EF) """
        context['COUNT'] += 1
        df = self._cmd.lchan.selected_file

	# The currently selected file (not the file we are going to export)
	# must always be an ADF or DF. From this starting point we select
	# the EF we want to export. To maintain consistency we will then
	# select the current DF again (see comment below).
        if not isinstance(df, CardDF):
            raise RuntimeError(
                "currently selected file %s is not a DF or ADF" % str(df))

        df_path_list = df.fully_qualified_path(True)
        df_path = df.fully_qualified_path_str(True)
        df_path_fid = df.fully_qualified_path_str(False)

        file_str = df_path + "/" + str(filename)
        self._cmd.poutput(boxed_heading_str(file_str))

        self._cmd.poutput("# directory: %s (%s)" % (df_path, df_path_fid))
        try:
            fcp_dec = self._cmd.lchan.select(filename, self._cmd)
            self._cmd.poutput("# file: %s (%s)" % (
                self._cmd.lchan.selected_file.name, self._cmd.lchan.selected_file.fid))

            structure = self._cmd.lchan.selected_file_structure()
            self._cmd.poutput("# structure: %s" % str(structure))
            self._cmd.poutput("# RAW FCP Template: %s" % str(self._cmd.lchan.selected_file_fcp_hex))
            self._cmd.poutput("# Decoded FCP Template: %s" % str(self._cmd.lchan.selected_file_fcp))

            for f in df_path_list:
                self._cmd.poutput("select " + str(f))
            self._cmd.poutput("select " + self._cmd.lchan.selected_file.name)

            if structure == 'transparent':
                if as_json:
                    result = self._cmd.lchan.read_binary_dec()
                    self._cmd.poutput("update_binary_decoded '%s'" % json.dumps(result[0], cls=JsonEncoder))
                else:
                    result = self._cmd.lchan.read_binary()
                    self._cmd.poutput("update_binary " + str(result[0]))
            elif structure == 'cyclic' or structure == 'linear_fixed':
                # Use number of records specified in select response
                num_of_rec = self._cmd.lchan.selected_file_num_of_rec()
                if num_of_rec:
                    for r in range(1, num_of_rec + 1):
                        if as_json:
                            result = self._cmd.lchan.read_record_dec(r)
                            self._cmd.poutput("update_record_decoded %d '%s'" % (r, json.dumps(result[0], cls=JsonEncoder)))
                        else:
                            result = self._cmd.lchan.read_record(r)
                            self._cmd.poutput("update_record %d %s" % (r, str(result[0])))

                # When the select response does not return the number of records, read until we hit the
                # first record that cannot be read.
                else:
                    r = 1
                    while True:
                        try:
                            if as_json:
                                result = self._cmd.lchan.read_record_dec(r)
                                self._cmd.poutput("update_record_decoded %d '%s'" % (r, json.dumps(result[0], cls=JsonEncoder)))
                            else:
                                result = self._cmd.lchan.read_record(r)
                                self._cmd.poutput("update_record %d %s" % (r, str(result[0])))
                        except SwMatchError as e:
                            # We are past the last valid record - stop
                            if e.sw_actual == "9402":
                                break
                            # Some other problem occurred
                            else:
                                raise e
                        r = r + 1
            elif structure == 'ber_tlv':
                tags = self._cmd.lchan.retrieve_tags()
                for t in tags:
                    result = self._cmd.lchan.retrieve_data(t)
                    (tag, l, val, remainer) = bertlv_parse_one(h2b(result[0]))
                    self._cmd.poutput("set_data 0x%02x %s" % (t, b2h(val)))
            else:
                raise RuntimeError(
                    'Unsupported structure "%s" of file "%s"' % (structure, filename))
        except Exception as e:
            bad_file_str = df_path + "/" + str(filename) + ", " + str(e)
            self._cmd.poutput("# bad file: %s" % bad_file_str)
            context['ERR'] += 1
            context['BAD'].append(bad_file_str)

        # When reading the file is done, make sure the parent file is
        # selected again. This will be the usual case, however we need
        # to check before since we must not select the same DF twice
        if df != self._cmd.lchan.selected_file:
            self._cmd.lchan.select(df.fid or df.aid, self._cmd)

        self._cmd.poutput("#")

    export_parser = argparse.ArgumentParser()
    export_parser.add_argument(
        '--filename', type=str, default=None, help='only export specific file')
    export_parser.add_argument(
        '--json', action='store_true', help='export as JSON (less reliable)')

    @cmd2.with_argparser(export_parser)
    def do_export(self, opts):
        """Export files to script that can be imported back later"""
        context = {'ERR': 0, 'COUNT': 0, 'BAD': [],
                   'DF_SKIP': 0, 'DF_SKIP_REASON': []}
        kwargs_export = {'as_json': opts.json}
        exception_str_add = ""

        if opts.filename:
            self.export_ef(opts.filename, context, **kwargs_export)
        else:
            try:
                self.walk(0, self.export_ef, None, context, **kwargs_export)
            except Exception as e:
                print("# Stopping early here due to exception: " + str(e))
                print("#")
                exception_str_add = ", also had to stop early due to exception:" + str(e)

        self._cmd.poutput(boxed_heading_str("Export summary"))

        self._cmd.poutput("# total files visited: %u" % context['COUNT'])
        self._cmd.poutput("# bad files:           %u" % context['ERR'])
        for b in context['BAD']:
            self._cmd.poutput("#  " + b)

        self._cmd.poutput("# skipped dedicated files(s): %u" %
                          context['DF_SKIP'])
        for b in context['DF_SKIP_REASON']:
            self._cmd.poutput("#  " + b)

        if context['ERR'] and context['DF_SKIP']:
            raise RuntimeError("unable to export %i elementary file(s) and %i dedicated file(s)%s" % (
                    context['ERR'], context['DF_SKIP'], exception_str_add))
        elif context['ERR']:
            raise RuntimeError(
                    "unable to export %i elementary file(s)%s" % (context['ERR'], exception_str_add))
        elif context['DF_SKIP']:
            raise RuntimeError(
                    "unable to export %i dedicated files(s)%s" % (context['ERR'], exception_str_add))

    def do_reset(self, opts):
        """Reset the Card."""
        atr = self._cmd.lchan.reset(self._cmd)
        self._cmd.poutput('Card ATR: %s' % atr)
        self._cmd.update_prompt()

    def do_desc(self, opts):
        """Display human readable file description for the currently selected file"""
        desc = self._cmd.lchan.selected_file.desc
        if desc:
            self._cmd.poutput(desc)
        else:
            self._cmd.poutput("no description available")

    def do_verify_adm(self, arg):
        """VERIFY the ADM1 PIN"""
        if arg:
            # use specified ADM-PIN
            pin_adm = sanitize_pin_adm(arg)
        else:
            # try to find an ADM-PIN if none is specified
            result = card_key_provider_get_field(
                'ADM1', key='ICCID', value=self._cmd.iccid)
            pin_adm = sanitize_pin_adm(result)
            if pin_adm:
                self._cmd.poutput(
                    "found ADM-PIN '%s' for ICCID '%s'" % (result, self._cmd.iccid))
            else:
                raise ValueError(
                    "cannot find ADM-PIN for ICCID '%s'" % (self._cmd.iccid))

        if pin_adm:
            self._cmd.card.verify_adm(h2b(pin_adm))
        else:
            raise ValueError("error: cannot authenticate, no adm-pin!")

    def do_cardinfo(self, opts):
        """Display information about the currently inserted card"""
        self._cmd.poutput("Card info:")
        self._cmd.poutput(" Name: %s" % self._cmd.card.name)
        self._cmd.poutput(" ATR: %s" % b2h(self._cmd.card._scc.get_atr()))
        self._cmd.poutput(" ICCID: %s" % self._cmd.iccid)
        self._cmd.poutput(" Class-Byte: %s" % self._cmd.card._scc.cla_byte)
        self._cmd.poutput(" Select-Ctrl: %s" % self._cmd.card._scc.sel_ctrl)
        self._cmd.poutput(" AIDs:")
        for a in self._cmd.rs.mf.applications:
                self._cmd.poutput("  %s" % a)

@with_default_category('ISO7816 Commands')
class Iso7816Commands(CommandSet):
    def __init__(self):
        super().__init__()

    def do_select(self, opts):
        """SELECT a File (ADF/DF/EF)"""
        if len(opts.arg_list) == 0:
            path = self._cmd.lchan.selected_file.fully_qualified_path_str(True)
            path_fid = self._cmd.lchan.selected_file.fully_qualified_path_str(False)
            self._cmd.poutput("currently selected file: %s (%s)" % (path, path_fid))
            return

        path = opts.arg_list[0]
        fcp_dec = self._cmd.lchan.select(path, self._cmd)
        self._cmd.update_prompt()
        self._cmd.poutput_json(fcp_dec)

    def complete_select(self, text, line, begidx, endidx) -> List[str]:
        """Command Line tab completion for SELECT"""
        index_dict = {1: self._cmd.lchan.selected_file.get_selectable_names()}
        return self._cmd.index_based_complete(text, line, begidx, endidx, index_dict=index_dict)

    def get_code(self, code):
        """Use code either directly or try to get it from external data source"""
        auto = ('PIN1', 'PIN2', 'PUK1', 'PUK2')

        if str(code).upper() not in auto:
            return sanitize_pin_adm(code)

        result = card_key_provider_get_field(
            str(code), key='ICCID', value=self._cmd.iccid)
        result = sanitize_pin_adm(result)
        if result:
            self._cmd.poutput("found %s '%s' for ICCID '%s'" %
                              (code.upper(), result, self._cmd.iccid))
        else:
            self._cmd.poutput("cannot find %s for ICCID '%s'" %
                              (code.upper(), self._cmd.iccid))
        return result

    verify_chv_parser = argparse.ArgumentParser()
    verify_chv_parser.add_argument(
        '--pin-nr', type=int, default=1, help='PIN Number, 1=PIN1, 2=PIN2 or custom value (decimal)')
    verify_chv_parser.add_argument(
        'pin_code', type=str, help='PIN code digits, \"PIN1\" or \"PIN2\" to get PIN code from external data source')

    @cmd2.with_argparser(verify_chv_parser)
    def do_verify_chv(self, opts):
        """Verify (authenticate) using specified CHV (PIN) code, which is how the specifications
        call it if you authenticate yourself using the specified PIN.  There usually is at least PIN1 and
        PIN2."""
        pin = self.get_code(opts.pin_code)
        (data, sw) = self._cmd.card._scc.verify_chv(opts.pin_nr, h2b(pin))
        self._cmd.poutput("CHV verification successful")

    unblock_chv_parser = argparse.ArgumentParser()
    unblock_chv_parser.add_argument(
        '--pin-nr', type=int, default=1, help='PUK Number, 1=PIN1, 2=PIN2 or custom value (decimal)')
    unblock_chv_parser.add_argument(
        'puk_code', type=str, help='PUK code digits \"PUK1\" or \"PUK2\" to get PUK code from external data source')
    unblock_chv_parser.add_argument(
        'new_pin_code', type=str, help='PIN code digits \"PIN1\" or \"PIN2\" to get PIN code from external data source')

    @cmd2.with_argparser(unblock_chv_parser)
    def do_unblock_chv(self, opts):
        """Unblock PIN code using specified PUK code"""
        new_pin = self.get_code(opts.new_pin_code)
        puk = self.get_code(opts.puk_code)
        (data, sw) = self._cmd.card._scc.unblock_chv(
            opts.pin_nr, h2b(puk), h2b(new_pin))
        self._cmd.poutput("CHV unblock successful")

    change_chv_parser = argparse.ArgumentParser()
    change_chv_parser.add_argument(
        '--pin-nr', type=int, default=1, help='PUK Number, 1=PIN1, 2=PIN2 or custom value (decimal)')
    change_chv_parser.add_argument(
        'pin_code', type=str, help='PIN code digits \"PIN1\" or \"PIN2\" to get PIN code from external data source')
    change_chv_parser.add_argument(
        'new_pin_code', type=str, help='PIN code digits \"PIN1\" or \"PIN2\" to get PIN code from external data source')

    @cmd2.with_argparser(change_chv_parser)
    def do_change_chv(self, opts):
        """Change PIN code to a new PIN code"""
        new_pin = self.get_code(opts.new_pin_code)
        pin = self.get_code(opts.pin_code)
        (data, sw) = self._cmd.card._scc.change_chv(
            opts.pin_nr, h2b(pin), h2b(new_pin))
        self._cmd.poutput("CHV change successful")

    disable_chv_parser = argparse.ArgumentParser()
    disable_chv_parser.add_argument(
        '--pin-nr', type=int, default=1, help='PIN Number, 1=PIN1, 2=PIN2 or custom value (decimal)')
    disable_chv_parser.add_argument(
        'pin_code', type=str, help='PIN code digits, \"PIN1\" or \"PIN2\" to get PIN code from external data source')

    @cmd2.with_argparser(disable_chv_parser)
    def do_disable_chv(self, opts):
        """Disable PIN code using specified PIN code"""
        pin = self.get_code(opts.pin_code)
        (data, sw) = self._cmd.card._scc.disable_chv(opts.pin_nr, h2b(pin))
        self._cmd.poutput("CHV disable successful")

    enable_chv_parser = argparse.ArgumentParser()
    enable_chv_parser.add_argument(
        '--pin-nr', type=int, default=1, help='PIN Number, 1=PIN1, 2=PIN2 or custom value (decimal)')
    enable_chv_parser.add_argument(
        'pin_code', type=str, help='PIN code digits, \"PIN1\" or \"PIN2\" to get PIN code from external data source')

    @cmd2.with_argparser(enable_chv_parser)
    def do_enable_chv(self, opts):
        """Enable PIN code using specified PIN code"""
        pin = self.get_code(opts.pin_code)
        (data, sw) = self._cmd.card._scc.enable_chv(opts.pin_nr, h2b(pin))
        self._cmd.poutput("CHV enable successful")

    def do_deactivate_file(self, opts):
        """Deactivate the currently selected EF"""
        (data, sw) = self._cmd.card._scc.deactivate_file()

    activate_file_parser = argparse.ArgumentParser()
    activate_file_parser.add_argument('NAME', type=str, help='File name or FID of file to activate')
    @cmd2.with_argparser(activate_file_parser)
    def do_activate_file(self, opts):
        """Activate the specified EF. This used to be called REHABILITATE in TS 11.11 for classic
        SIM.  You need to specify the name or FID of the file to activate."""
        (data, sw) = self._cmd.lchan.activate_file(opts.NAME)

    def complete_activate_file(self, text, line, begidx, endidx) -> List[str]:
        """Command Line tab completion for ACTIVATE FILE"""
        index_dict = {1: self._cmd.lchan.selected_file.get_selectable_names()}
        return self._cmd.index_based_complete(text, line, begidx, endidx, index_dict=index_dict)

    open_chan_parser = argparse.ArgumentParser()
    open_chan_parser.add_argument(
        'chan_nr', type=int, default=0, help='Channel Number')

    @cmd2.with_argparser(open_chan_parser)
    def do_open_channel(self, opts):
        """Open a logical channel."""
        (data, sw) = self._cmd.card._scc.manage_channel(
            mode='open', lchan_nr=opts.chan_nr)

    close_chan_parser = argparse.ArgumentParser()
    close_chan_parser.add_argument(
        'chan_nr', type=int, default=0, help='Channel Number')

    @cmd2.with_argparser(close_chan_parser)
    def do_close_channel(self, opts):
        """Close a logical channel."""
        (data, sw) = self._cmd.card._scc.manage_channel(
            mode='close', lchan_nr=opts.chan_nr)

    def do_status(self, opts):
        """Perform the STATUS command."""
        fcp_dec = self._cmd.lchan.status()
        self._cmd.poutput_json(fcp_dec)


class Proact(ProactiveHandler):
    def receive_fetch(self, pcmd: ProactiveCommand):
        # print its parsed representation
        print(pcmd.decoded)
        # TODO: implement the basics, such as SMS Sending, ...



option_parser = argparse.ArgumentParser(prog='pySim-shell', description='interactive SIM card shell',
                                        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
argparse_add_reader_args(option_parser)

global_group = option_parser.add_argument_group('General Options')
global_group.add_argument('--script', metavar='PATH', default=None,
                          help='script with pySim-shell commands to be executed automatically at start-up')
global_group.add_argument('--csv', metavar='FILE',
                          default=None, help='Read card data from CSV file')
global_group.add_argument("--card_handler", dest="card_handler_config", metavar="FILE",
                          help="Use automatic card handling machine")

adm_group = global_group.add_mutually_exclusive_group()
adm_group.add_argument('-a', '--pin-adm', metavar='PIN_ADM1', dest='pin_adm', default=None,
                       help='ADM PIN used for provisioning (overwrites default)')
adm_group.add_argument('-A', '--pin-adm-hex', metavar='PIN_ADM1_HEX', dest='pin_adm_hex', default=None,
                       help='ADM PIN used for provisioning, as hex string (16 characters long)')


if __name__ == '__main__':

    # Parse options
    opts = option_parser.parse_args()

    # If a script file is specified, be sure that it actually exists
    if opts.script:
        if not os.access(opts.script, os.R_OK):
            print("Invalid script file!")
            sys.exit(2)

    # Register csv-file as card data provider, either from specified CSV
    # or from CSV file in home directory
    csv_default = str(Path.home()) + "/.osmocom/pysim/card_data.csv"
    if opts.csv:
        card_key_provider_register(CardKeyProviderCsv(opts.csv))
    if os.path.isfile(csv_default):
        card_key_provider_register(CardKeyProviderCsv(csv_default))

    # Init card reader driver
    sl = init_reader(opts, proactive_handler = Proact())
    if sl is None:
        exit(1)

    # Create command layer
    scc = SimCardCommands(transport=sl)

    # Create a card handler (for bulk provisioning)
    if opts.card_handler_config:
        ch = CardHandlerAuto(None, opts.card_handler_config)
    else:
        ch = CardHandler(sl)

    # Detect and initialize the card in the reader. This may fail when there
    # is no card in the reader or the card is unresponsive. PysimApp is
    # able to tolerate and recover from that.
    try:
        rs, card = init_card(sl)
        app = PysimApp(card, rs, sl, ch, opts.script)
    except:
        print("Card initialization failed with an exception:")
        print("---------------------8<---------------------")
        traceback.print_exc()
        print("---------------------8<---------------------")
        print("(you may still try to recover from this manually by using the 'equip' command.)")
        print(
            " it should also be noted that some readers may behave strangely when no card")
        print(" is inserted.)")
        print("")
        app = PysimApp(card, None, sl, ch, opts.script)

    # If the user supplies an ADM PIN at via commandline args authenticate
    # immediately so that the user does not have to use the shell commands
    pin_adm = sanitize_pin_adm(opts.pin_adm, opts.pin_adm_hex)
    if pin_adm:
        if not card:
            print("Card error, cannot do ADM verification with supplied ADM pin now.")
        try:
            card.verify_adm(h2b(pin_adm))
        except Exception as e:
            print(e)

    app.cmdloop()
