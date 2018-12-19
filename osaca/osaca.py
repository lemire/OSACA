#!/usr/bin/env python3

import argparse
import sys
import os
import io
import re
import subprocess
from datetime import datetime

import pandas as pd
import numpy as np

from osaca.param import Register, MemAddr, Parameter
from osaca.eu_sched import Scheduler
from osaca.testcase import Testcase

DATA_DIR = os.path.expanduser('~') + '/.osaca/'


class OSACA(object):
    srcCode = None
    tp_list = False
    # Variables for checking lines
    numSeps = 0
    indentChar = ''
    sem = 0

    # Variables for creating output
    longestInstr = 30
    machine_readable = False
    # Constants
    ASM_LINE = re.compile(r'\s[0-9a-f]+[:]')
    # Matches every variation of the IACA start marker
    IACA_SM = re.compile(r'\s*movl[ \t]+\$111[ \t]*,[ \t]*%ebx.*\n\s*\.byte[ \t]+100.*'
                         r'((,[ \t]*103.*((,[ \t]*144)|(\n\s*\.byte[ \t]+144)))|(\n\s*\.byte'
                         r'[ \t]+103.*((,[ \t]*144)|(\n\s*\.byte[ \t]+144))))')
    # Matches every variation of the IACA end marker
    IACA_EM = re.compile(r'\s*movl[ \t]+\$222[ \t]*,[ \t]*%ebx.*\n\s*\.byte[ \t]+100.*'
                         r'((,[ \t]*103.*((,[ \t]*144)|(\n\s*\.byte[ \t]+144)))|(\n\s*\.byte'
                         r'[ \t]+103.*((,[ \t]*144)|(\n\s*\.byte[ \t]+144))))')

    VALID_ARCHS = ['SNB', 'IVB', 'HSW', 'BDW', 'SKL', 'ZEN']

    def __init__(self, arch, file_path, output=sys.stdout):
        # Check architecture
        if arch not in self.VALID_ARCHS:
            raise ValueError("Invalid architecture ({!r}), must be one of {}.".format(
                arch, self.VALID_ARCHS))
        self.arch = arch

        self.file_path = file_path
        self.instr_forms = []
        self.file_output = output
        # Check if data files are already in usr dir, otherwise create them
        if not os.path.isdir(os.path.join(DATA_DIR, 'data')):
            print('Copying files in user directory...', file=self.file_output, end='')
            os.makedirs(os.path.join(DATA_DIR, 'data'))
            subprocess.call(['cp', '-r',
                             '/'.join(os.path.realpath(__file__).split('/')[:-1]) + '/data',
                             DATA_DIR])
            print(' Done!', file=self.file_output)

        # Check for database for the chosen architecture
        self.df = self.read_csv()

    # -----------------main functions depending on arguments--------------------
    def include_ibench(self):
        """
        Read ibench output and include it in the architecture specific csv file.
        """
        if not self.check_file():
            print('Invalid file path or file format.', file=sys.stderr)
            sys.exit(1)
        # Create sequence of numbers and their reciprocals for validate the measurements
        cyc_list, reci_list = self.create_sequences()
        # print('Everything seems fine! Let\'s start!', file=self.file_output)
        new_data = []
        added_vals = 0
        for line in self.srcCode:
            if 'Using frequency' in line or len(line) == 0:
                continue
            column = 'LT'
            instr = line.split()[0][:-1]
            if 'TP' in line:
                # We found a command with a throughput value. Get instruction and the number of
                # clock cycles and remove the '-TP' suffix.
                column = 'TP'
                instr = instr[:-3]
            # Otherwise it is a latency value. Nothing to do.
            clk_cyc = float(line.split()[1])
            clk_cyc_tmp = clk_cyc
            clk_cyc = self.validate_val(clk_cyc, instr, True if (column == 'TP') else False,
                                        cyc_list, reci_list)
            txt_output = (clk_cyc_tmp == clk_cyc)
            val = -2
            new = False
            try:
                entry = self.df.loc[lambda df, inst=instr: df.instr == inst, column]
                val = entry.values[0]
                # If val is -1 (= not filled with a valid value) add it immediately
                if val == -1:
                    self.df.set_value(entry.index[0], column, clk_cyc)
                    added_vals += 1
                    continue
            except IndexError:
                # Instruction not in database yet --> add it
                new = True
                # First check if LT or TP value has already been added before
                for i, item in enumerate(new_data):
                    if instr in item:
                        if column == 'TP':
                            new_data[i][1] = clk_cyc
                        elif column == 'LT':
                            new_data[i][2] = clk_cyc
                        new = False
                        break
                if new and column == 'TP':
                    new_data.append([instr, clk_cyc, '-1', (-1,)])
                elif new and column == 'LT':
                    new_data.append([instr, '-1', clk_cyc, (-1,)])
                new = True
                added_vals += 1
            if not new and abs((val / np.float64(clk_cyc)) - 1) > 0.05:
                print('Different measurement for {} ({}): {}(old) vs. '.format(instr, column, val)
                      + '{}(new)\nPlease check for correctness '.format(clk_cyc)
                      + '(no changes were made).', file=self.file_output)
                txt_output = True
            if txt_output:
                print('', file=self.file_output)
        # Now merge the DataFrames and write new csv file
        self.df = self.df.append(pd.DataFrame(new_data, columns=['instr', 'TP', 'LT', 'ports']),
                                 ignore_index=True)
        self.write_csv()
        print('ibench output included successfully in data file .', file=self.file_output)
        print('{} values were added.'.format(added_vals), file=self.file_output)

    def inspect_binary(self):
        """
        Main function of OSACA. Inspect binary file and create analysis.
        """
        # Check args and exit program if something's wrong
        if not self.check_elffile():
            print('Invalid file path or file format. Not an ELF file.', file=sys.stderr)
            sys.exit(1)

        # print('Everything seems fine! Let\'s start checking!', file=self.file_output)

        for i, line in enumerate(self.srcCode):
            if i == 0:
                self.check_line(line, True)
            else:
                self.check_line(line)
        output = self.create_output(self.tp_list, True, self.machine_readable)
        if self.machine_readable:
            return output
        else:
            print(output, file=self.file_output)

    def inspect_with_iaca(self):
        """
        Main function of OSACA with IACA markers instead of OSACA marker.
        Inspect binary file and create analysis.
        """
        # Check if input file is a binary or assembly file
        binary_file = True
        if not self.check_elffile():
            binary_file = False
            if not self.check_file(True):
                print('Invalid file path or file format.', file=sys.stderr)
                sys.exit(1)

        # print('Everything seems fine! Let\'s start checking!', file=self.file_output)
        if binary_file:
            self.iaca_bin()
        else:
            self.iaca_asm()
        output = self.create_output(self.tp_list, True, self.machine_readable)
        if self.machine_readable:
            return output
        else:
            print(output, file=self.file_output)

    # --------------------------------------------------------------------------

    def check_file(self, iaca_flag=False):
        """
        Check if the given filepath exists and store file data in attribute
        srcCode.

        Parameters
        ----------
        iaca_flag : bool
            store file data as a string in attribute srcCode if True,
            store it as a list of strings (lines) if False (default False)

        Returns
        -------
        bool
            True    if file exists
            False   if file does not exist

        """
        if os.path.isfile(self.file_path):
            self.store_src_code(iaca_flag)
            return True
        return False

    def store_src_code(self, iaca_flag=False):
        """
        Load arbitrary file in class attribute srcCode.

        Parameters
        ----------
        iaca_flag : bool
                store file data as a string in attribute srcCode if True,
                store it as a list of strings (lines) if False (default False)
        """
        with open(self.file_path, 'r') as f:
            self.srcCode = f.read()

        if iaca_flag:
            return
        self.srcCode = self.srcCode.split('\n')

    def read_csv(self):
        """
        Read architecture dependent CSV from data directory.

        Returns
        -------
        DataFrame
            CSV as DataFrame object
        """
        # curr_dir = '/'.join(os.path.realpath(__file__).split('/')[:-1])
        return pd.read_csv(DATA_DIR + 'data/' + self.arch.lower() + '_data.csv')

    def write_csv(self):
        """
        Write architecture DataFrame as CSV into data directory.
        """
        # curr_dir = '/'.join(os.path.realpath(__file__).split('/')[:-1])
        csv = self.df.to_csv(index=False)
        with open(DATA_DIR + 'data/' + self.arch.lower() + '_data.csv', 'w') as f:
            f.write(csv)

    def create_sequences(self, end=101):
        """
        Create list of integers from 1 to end and list of their reciprocals.

        Parameters
        ----------
        end : int
            End value for list of integers (default 101)

        Returns
        -------
        [int]
            cyc_list of integers
        [float]
            reci_list of floats
        """
        cyc_list = []
        reci_list = []
        for i in range(1, end):
            cyc_list.append(i)
            reci_list.append(1 / i)
        return cyc_list, reci_list

    def validate_val(self, clk_cyc, instr, is_tp, cyc_list, reci_list):
        """
        Validate given clock cycle clk_cyc and return rounded value in case of
        success.

        A succeeded validation means the clock cycle clk_cyc is only 5% higher or
        lower than an integer value from cyc_list or - if clk_cyc is a throughput
        value - 5% higher or lower than a reciprocal from the reci_list.

        Parameters
        ----------
        clk_cyc : float
            Clock cycle to validate
        instr : str
            Instruction for warning output
        is_tp : bool
            True if a throughput value is to check, False for a latency value
        cyc_list : [int]
            Cycle list for validating
        reci_list : [float]
            Reciprocal cycle list for validating

        Returns
        -------
        float
            Clock cycle, either rounded to an integer or its reciprocal or the
            given clk_cyc parameter
        """
        column = 'LT'
        if is_tp:
            column = 'TP'
        for i in range(0, len(cyc_list)):
            if cyc_list[i] * 1.05 > float(clk_cyc) > cyc_list[i] * 0.95:
                # Value is probably correct, so round it to the estimated value
                return cyc_list[i]
            # Check reciprocal only if it is a throughput value
            elif is_tp and reci_list[i] * 1.05 > float(clk_cyc) > reci_list[i] * 0.95:
                # Value is probably correct, so round it to the estimated value
                return reci_list[i]
        # No value close to an integer or its reciprocal found, we assume the
        # measurement is incorrect
        print('Your measurement for {} ({}) is probably wrong. '.format(instr, column)
              + 'Please inspect your benchmark!', file=self.file_output)
        print('The program will continue with the given value', file=self.file_output)
        return clk_cyc

    def iaca_bin(self):
        """
        Extract instruction forms out of binary file using IACA markers.
        """
        self.CODE_MARKER = r'fs addr32 nop'
        part1 = re.compile(r'64\s+fs')
        part2 = re.compile(r'67 90\s+addr32 nop')
        for line in self.srcCode:
            # Check if marker is in line
            if self.CODE_MARKER in line:
                self.sem += 1
            elif re.search(part1, line) or re.search(part2, line):
                self.sem += 0.5
            elif self.sem == 1:
                # We're in the marked code snippet
                # Check if the line is ASM code
                match = re.search(self.ASM_LINE, line)
                if match:
                    # Further analysis of instructions
                    # Check if there are comments in line
                    if r'//' in line:
                        continue
                    # Do the same instruction check as for the OSACA marker line check
                    self.check_instr(''.join(re.split(r'\t', line)[-1:]))
            elif self.sem == 2:
                # Not in the loop anymore. Due to the fact it's the IACA marker we can stop here
                # After removing the last line which belongs to the IACA marker
                del self.instr_forms[-1:]
                # if(is_2_lines):
                # The marker is splitted into two lines, therefore delete another line
                #    del self.instr_forms[-1:]
                return

    def iaca_asm(self):
        """
        Extract instruction forms out of assembly file using IACA markers.
        """
        # Extract the code snippet surround by the IACA markers
        code = self.srcCode
        # Search for the start marker
        match = re.match(self.IACA_SM, code)
        while not match:
            code = code.split('\n', 1)
            if len(code) > 1:
                code = code[1]
            else:
                raise ValueError("No IACA-style markers found in assembly code.")
            match = re.match(self.IACA_SM, code)
        # Search for the end marker
        code = (code.split('144', 1)[1]).split('\n', 1)[1]
        res = ''
        match = re.match(self.IACA_EM, code)
        while not match:
            res += code.split('\n', 1)[0] + '\n'
            code = code.split('\n', 1)[1]
            match = re.match(self.IACA_EM, code)
        # Split the result by line go on like with OSACA markers
        res = res.split('\n')
        for line in res:
            line = line.split('#')[0]
            line = line.lstrip()
            if len(line) == 0 or '//' in line or line.startswith('..'):
                continue
            self.check_instr(line)

    def check_instr(self, instr):
        """
        Inspect instruction for its parameters and add it to the instruction forms
        pool instr_form.

        Parameters
        ----------
        instr : str
            Instruction as string
        """
        # Check for strange clang padding bytes
        while instr.startswith('data32'):
            instr = instr[7:]
        # Separate mnemonic and operands
        mnemonic = instr.split()[0]
        params = ''.join(instr.split()[1:])
        # Check if line is not only a byte
        empty_byte = re.compile(r'[0-9a-f]{2}')
        if re.match(empty_byte, mnemonic) and len(mnemonic) == 2:
            return
        # Check if there's one or more operands and store all in a list
        param_list = self.flatten(self.separate_params(params))
        param_list_types = list(param_list)
        # Check operands and separate them by IMMEDIATE (IMD), REGISTER (REG),
        # MEMORY (MEM) or LABEL(LBL)
        for i in range(len(param_list)):
            op = param_list[i]
            if len(op) <= 0:
                op = Parameter('NONE')
            elif op[0] == '$':
                op = Parameter('IMD')
            elif op[0] == '%' and '(' not in op:
                j = len(op)
                opmask = False
                if '{' in op:
                    j = op.index('{')
                    opmask = True
                op = Register(op[1:j], opmask)
            elif '<' in op or op.startswith('.'):
                op = Parameter('LBL')
            else:
                op = MemAddr(op, )
            param_list[i] = str(op)
            param_list_types[i] = op
        # Add to list
        instr = instr.rstrip()
        if len(instr) > self.longestInstr:
            self.longestInstr = len(instr)
        instr_form = [mnemonic] + list(reversed(param_list_types)) + [instr]
        self.instr_forms.append(instr_form)
        # If flag is set, create testcase for instruction form
        # Do this in reversed param list order, du to the fact it's intel syntax
        # Only create benchmark if no label (LBL) is part of the operands
        if 'LBL' in param_list or '' in param_list:
            return
        tc = Testcase(mnemonic, list(reversed(param_list_types)), '32')
        # Only write a testcase if it not already exists or already in data file
        writeTP, writeLT = tc.is_in_dir()
        inDB = len(self.df.loc[lambda df: df.instr == tc.get_entryname()])
        if inDB == 0:
            tc.write_testcase(not writeTP, not writeLT)

    def separate_params(self, params):
        """
        Delete comments, separates parameters and return them as a list.

        Parameters
        ----------
        params : str
            Splitted line after mnemonic

        Returns
        -------
        [[...[str]]]
            Nested list of strings. The number of nest levels depend on the
            number of parametes given.
        """
        param_list = [params]
        if ',' in params:
            if ')' in params:
                if params.index(')') < len(params) - 1 and params[params.index(')') + 1] == ',':
                    i = params.index(')') + 1
                elif params.index('(') < params.index(','):
                    return param_list
                else:
                    i = params.index(',')
            else:
                i = params.index(',')
            param_list = [params[:i], self.separate_params(params[i + 1:])]
        elif '#' in params:
            i = params.index('#')
            param_list = [params[:i]]
        return param_list

    def flatten(self, l):
        """
        Flatten a nested list of strings.

        Parameters
        ----------
        l : [[...[str]]]
            Nested list of strings

        Returns
        -------
        [str]
            List of strings
        """
        if not l:
            return l
        if isinstance(l[0], list):
            return self.flatten(l[0]) + self.flatten(l[1:])
        return l[:1] + self.flatten(l[1:])

    def create_output(self, tp_list=False, pr_sched=True, machine_readable=False):
        """
        Creates output of analysed file including a time stamp.

        Parameters
        ----------
        tp_list : bool
            Boolean for indicating the need for the throughput list as output
            (default False)
        pr_sched : bool
            Boolean for indicating the need for predicting a scheduling
            (default True)

        Returns
        -------
        str
            OSACA output
        """
        # Check the output alignment depending on the longest instruction
        if self.longestInstr > 70:
            self.longestInstr = 70
        horiz_line = self.create_horiz_sep()
        # Write general information about the benchmark
        output = '--{}\n| Architecture:\t\t{}\n|\n'.format(
            horiz_line, self.arch)
        if tp_list:
            output += self.create_tp_list(horiz_line)
        if pr_sched:
            output += '\n\n'
            sched = Scheduler(self.arch, self.instr_forms)
            sched_output, port_binding = sched.new_schedule(machine_readable)
            # if machine_readable, we're already done here
            if machine_readable:
                return sched_output
            binding = sched.get_port_binding(port_binding)
            output += sched.get_report_info() + '\n' + binding + '\n\n' + sched_output
            block_tp = round(max(port_binding), 2)
            output += 'Total number of estimated throughput: ' + str(block_tp)
        return output

    def create_horiz_sep(self):
        """
        Calculate and return horizontal separator line.

        Returns
        -------
        str
            Horizontal separator line
        """
        return '-' * (self.longestInstr + 8)

    def create_tp_list(self, horiz_line):
        """
        Create list of instruction forms with the proper throughput value.

        Parameter
        ---------
        horiz_line : str
            Calculated horizontal line for nice alignement

        Returns
        -------
        str
            Throughput list output for printing
        """
        warning = False
        ws = ' ' * (len(horiz_line) - 23)

        output = '\n| INSTRUCTION{}CLOCK CYCLES\n| {}\n|\n'.format(ws, horiz_line)
        # Check for the throughput data in CSV
        for elem in self.instr_forms:
            op_ext = []
            for i in range(1, len(elem) - 1):
                if isinstance(elem[i], Register) and elem[i].reg_type == 'GPR':
                    optmp = 'r' + str(elem[i].size)
                elif isinstance(elem[i], MemAddr):
                    optmp = 'mem'
                else:
                    optmp = str(elem[i]).lower()
                op_ext.append(optmp)
            operands = '_'.join(op_ext)
            # Now look up the value in the dataframe
            # Check if there is a stored throughput value in database
            import warnings
            warnings.filterwarnings("ignore", 'This pattern has match groups')
            series = self.df['instr'].str.contains(elem[0] + '-' + operands)
            if True in series.values:
                # It's a match!
                not_found = False
                try:
                    tp = self.df[self.df.instr == elem[0] + '-' + operands].TP.values[0]
                except IndexError:
                    # Something went wrong
                    print('Error while fetching data from data file', file=self.file_output)
                    continue
            # Did not found the exact instruction form.
            # Try to find the instruction form for register operands only
            else:
                op_ext_regs = []
                for operand in op_ext:
                    try:
                        # regTmp = Register(operand)
                        # Create Register only to see if it is one
                        Register(operand)
                        op_ext_regs.append(True)
                    except KeyError:
                        op_ext_regs.append(False)
                if True not in op_ext_regs:
                    # No register in whole instr form. How can I find out what regsize we need?
                    print('Feature not included yet: ', end='', file=self.file_output)
                    print(elem[0] + ' for ' + operands, file=self.file_output)
                    tp = 0
                    warning = True
                    num_whitespaces = self.longestInstr - len(elem[-1])
                    ws = ' ' * num_whitespaces + '|  '
                    n_f = ' ' * (5 - len(str(tp))) + '*'
                    data = '| ' + elem[-1] + ws + str(tp) + n_f + '\n'
                    output += data
                    continue
                if op_ext_regs[0] is False:
                    # Instruction stores result in memory. Check for storing in register instead.
                    if len(op_ext) > 1:
                        if op_ext_regs[1] is True:
                            op_ext[0] = op_ext[1]
                        elif len(op_ext) > 2:
                            if op_ext_regs[2] is True:
                                op_ext[0] = op_ext[2]
                if len(op_ext_regs) == 2 and op_ext_regs[1] is False:
                    # Instruction loads value from memory and has only two operands. Check for
                    # loading from register instead
                    if op_ext_regs[0] is True:
                        op_ext[1] = op_ext[0]
                if len(op_ext_regs) == 3 and op_ext_regs[2] is False:
                    # Instruction loads value from memory and has three operands. Check for loading
                    # from register instead
                    op_ext[2] = op_ext[0]
                operands = '_'.join(op_ext)
                # Check for register equivalent instruction
                series = self.df['instr'].str.contains(elem[0] + '-' + operands)
                if True in series.values:
                    # It's a match!
                    not_found = False
                    try:
                        tp = self.df[self.df.instr == elem[0] + '-' + operands].TP.values[0]
                    except IndexError:
                        # Something went wrong
                        print('Error while fetching data from data file', file=self.file_output)
                        continue
                # Did not found the register instruction form. Set warning and go on with
                # throughput 0
                else:
                    tp = 0
                    not_found = True
                    warning = True
            # Check the alignement again
            num_whitespaces = self.longestInstr - len(elem[-1])
            ws = ' ' * num_whitespaces + '|  '
            n_f = ''
            if not_found:
                n_f = ' ' * (5 - len(str(tp))) + '*'
            data = '| ' + elem[-1] + ws + '{:3.2f}'.format(tp) + n_f + '\n'
            output += data
        # Finally end the list of  throughput values
        output += '| ' + horiz_line + '\n'
        if warning:
            output += ('\n\n* There was no throughput value found  for the specific instruction '
                       'form.\n  Please create a testcase via the create_testcase-method or add a '
                       'value manually.')
        return output


# ------------------------------------------------------------------------------
# Stolen from pip
def __read(*names, **kwargs):
    with io.open(
            os.path.join(os.path.dirname(__file__), *names),
            encoding=kwargs.get("encoding", "utf8")
    ) as fp:
        return fp.read()


# Stolen from pip
def __find_version(*file_paths):
    version_file = __read(*file_paths)
    version_match = re.search(r"^__version__ = ['\"]([^'\"]*)['\"]", version_file, re.M)
    if version_match:
        return version_match.group(1)
    raise RuntimeError('Unable to find version string.')


# ------------Main method--------------
def main():
    # Parse args
    parser = argparse.ArgumentParser(description='Analyzes a marked innermost loop snippet'
                                                 'for a given architecture type and prints out the '
                                                 'estimated average throughput.')
    parser.add_argument('-V', '--version', action='version',
                        version='%(prog)s ' + __find_version('__init__.py'))
    parser.add_argument('--arch', type=str, required=True,
                        help='define architecture (SNB, IVB, HSW, BDW, SKL, ZEN)')
    parser.add_argument('--tp-list', action='store_true',
                        help='print an additional list of all throughput values for the kernel')
    group = parser.add_mutually_exclusive_group(required=False)
    group.add_argument('-i', '--include-ibench', action='store_true',
                       help='includes the given values in form of the output of ibench in the'
                            'data file')
    group.add_argument('--iaca', action='store_true',
                       help='search for IACA markers instead the OSACA marker')
    group.add_argument('--insert-marker', '-m', action='store_true',
                       help='try to find blocks probably corresponding to loops in assembly and'
                            'insert IACA marker')
    parser.add_argument('-l', '--list-output', dest='machine_readable', action='store_true',
                        help='returns output as machine readable list of lists')
    parser.add_argument('filepath', type=str, help='path to object (Binary, ASM, CSV)')

    # Store args in global variables
    args = parser.parse_args()

    # Create OSACA object
    osaca = OSACA(args.arch.upper(), args.filepath)
    if args.tp_list:
        osaca.tp_list = True
    if args.machine_readable:
        osaca.machine_readable = True
        osaca.output = None

    if args.include_ibench:
        try:
            osaca.include_ibench()
        except UnboundLocalError:
            print('Please specify an architecture.', file=sys.stderr)
    elif args.iaca:
        try:
            return osaca.inspect_with_iaca()
        except UnboundLocalError:
            print('Please specify an architecture.', file=sys.stderr)
    elif args.insert_marker:
        try:
            from kerncraft import iaca
        except ImportError:
            print("ImportError: Module kerncraft not installed. Use 'pip install --user "
                  "kerncraft' for installation.\nFor more information see "
                  "https://github.com/RRZE-HPC/kerncraft", file=sys.stderr)
            sys.exit(1)
        # Change due to newer kerncraft version (hopefully temporary)
        # iaca.iaca_instrumentation(input_file=filepath, output_file=filepath,
        #                          block_selection='manual', pointer_increment=1)
        with open(args.filepath, 'r') as f_in, open(args.filepath[:-2] + '-iaca.s', 'w') as f_out:
            iaca.iaca_instrumentation(input_file=f_in, output_file=f_out,
                                      block_selection='manual', pointer_increment=1)
    else:
        raise Exception("Not clear what to do.")


# ------------Main method--------------
if __name__ == '__main__':
    main()
