#!/usr/bin/env python
#
# esp-idf serial output monitor tool. Does some helpful things:
# - Looks up hex addresses in ELF file with addr2line
# - Reset ESP32 via serial RTS line (Ctrl-T Ctrl-R)
# - Run flash build target to rebuild and flash entire project (Ctrl-T Ctrl-F)
# - Run app-flash build target to rebuild and flash app only (Ctrl-T Ctrl-A)
# - If gdbstub output is detected, gdb is automatically loaded
# - If core dump output is detected, it is converted to a human-readable report
#   by espcoredump.py.
#
# Copyright 2015-2021 Espressif Systems (Shanghai) CO LTD
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#
# Contains elements taken from miniterm "Very simple serial terminal" which
# is part of pySerial. https://github.com/pyserial/pyserial
# (C)2002-2015 Chris Liechti <cliechti@gmx.net>
#
# Originally released under BSD-3-Clause license.
#

from __future__ import division, print_function, unicode_literals

import argparse
import codecs
import os
import re
import subprocess
import threading
import time
from builtins import bytes, object

try:
    from typing import List, Optional, Union  # noqa
except ImportError:
    pass

from idf_monitor_base.chip_specific_config import get_chip_config
from idf_monitor_base.console_parser import ConsoleParser, prompt_next_action
from idf_monitor_base.console_reader import ConsoleReader
from idf_monitor_base.constants import (CMD_APP_FLASH, CMD_ENTER_BOOT, CMD_MAKE, CMD_OUTPUT_TOGGLE, CMD_RESET,
                                        CMD_STOP, CMD_TOGGLE_LOGGING, CMD_TOGGLE_TIMESTAMPS, CTRL_H,
                                        DEFAULT_PRINT_FILTER, DEFAULT_TOOLCHAIN_PREFIX, MATCH_PCADDR,
                                        PANIC_DECODE_BACKTRACE, PANIC_DECODE_DISABLE, PANIC_END, PANIC_IDLE,
                                        PANIC_READING, PANIC_STACK_DUMP, PANIC_START, TAG_CMD, TAG_KEY, TAG_SERIAL,
                                        TAG_SERIAL_FLUSH)
from idf_monitor_base.coredump import COREDUMP_DECODE_DISABLE, COREDUMP_DECODE_INFO, CoreDump
from idf_monitor_base.exceptions import SerialStopException
from idf_monitor_base.gdbhelper import GDBHelper
from idf_monitor_base.line_matcher import LineMatcher
from idf_monitor_base.output_helpers import Logger, lookup_pc_address, normal_print, yellow_print
from idf_monitor_base.serial_reader import SerialReader
from idf_monitor_base.web_socket_client import WebSocketClient
from serial.tools import miniterm

try:
    import queue  # noqa
except ImportError:
    import Queue as queue  # type: ignore  # noqa

import shlex
import sys

import serial
import serial.tools.list_ports
# Windows console stuff
from idf_monitor_base.ansi_color_converter import get_converter

key_description = miniterm.key_description


class Monitor(object):
    """
    Monitor application main class.

    This was originally derived from miniterm.Miniterm, but it turned out to be easier to write from scratch for this
    purpose.

    Main difference is that all event processing happens in the main thread, not the worker threads.
    """

    def __init__(
        self,
        serial_instance,  # type: serial.Serial
        elf_file,  # type: str
        print_filter,  # type: str
        make='make',  # type: str
        encrypted=False,  # type: bool
        toolchain_prefix=DEFAULT_TOOLCHAIN_PREFIX,  # type: str
        eol='CRLF',  # type: str
        decode_coredumps=COREDUMP_DECODE_INFO,  # type: str
        decode_panic=PANIC_DECODE_DISABLE,  # type: str
        target='esp32',  # type: str
        websocket_client=None,  # type: WebSocketClient
        enable_address_decoding=True,  # type: bool
        timestamps=False,  # type: bool
        timestamp_format=''  # type: str
    ):
        super(Monitor, self).__init__()
        self.event_queue = queue.Queue()  # type: queue.Queue
        self.cmd_queue = queue.Queue()  # type: queue.Queue
        self.console = miniterm.Console()
        self.enable_address_decoding = enable_address_decoding

        sys.stderr = get_converter(sys.stderr, decode_output=True)
        self.console.output = get_converter(self.console.output)
        self.console.byte_output = get_converter(self.console.byte_output)

        socket_mode = serial_instance.port.startswith(
            'socket://')  # testing hook - data from serial can make exit the monitor
        self.serial = serial_instance

        self.console_parser = ConsoleParser(eol)
        self.console_reader = ConsoleReader(self.console, self.event_queue, self.cmd_queue, self.console_parser,
                                            socket_mode)
        self.serial_reader = SerialReader(self.serial, self.event_queue)
        self.elf_file = elf_file
        if not os.path.exists(make):
            # allow for possibility the "make" arg is a list of arguments (for idf.py)
            self.make = shlex.split(make)  # type: Union[str, List[str]]
        else:
            self.make = make
        self.encrypted = encrypted
        self.toolchain_prefix = toolchain_prefix
        self.websocket_client = websocket_client
        self.target = target

        # internal state
        self._last_line_part = b''
        self._pc_address_buffer = b''
        self._line_matcher = LineMatcher(print_filter)
        self._invoke_processing_last_line_timer = None  # type: Optional[threading.Timer]
        self._force_line_print = False
        self._serial_check_exit = socket_mode
        self._decode_panic = decode_panic
        self._reading_panic = PANIC_IDLE
        self._panic_buffer = b''
        self.start_cmd_sent = False
        self.gdb_helper = GDBHelper(self.toolchain_prefix, self.websocket_client, self.elf_file, self.serial.port,
                                    self.serial.baudrate)

        self.logger = Logger(self.elf_file, self.console, timestamps, timestamp_format)
        self.coredump = CoreDump(decode_coredumps, self.event_queue, self.logger, self.websocket_client, self.elf_file)

    def invoke_processing_last_line(self):
        # type: () -> None
        self.event_queue.put((TAG_SERIAL_FLUSH, b''), False)

    def main_loop(self):
        # type: () -> None
        self.console_reader.start()
        self.serial_reader.start()
        self.gdb_helper.gdb_exit = False
        self.start_cmd_sent = False
        try:
            while self.console_reader.alive and self.serial_reader.alive:
                try:
                    if self.gdb_helper.gdb_exit:
                        self.gdb_helper.gdb_exit = False

                        time.sleep(0.3)
                        try:
                            # Continue the program after exit from the GDB
                            self.serial.write(codecs.encode('+$c#63'))
                            self.start_cmd_sent = True
                        except serial.SerialException:
                            pass  # this shouldn't happen, but sometimes port has closed in serial thread
                        except UnicodeEncodeError:
                            pass  # this can happen if a non-ascii character was passed, ignoring

                    try:
                        item = self.cmd_queue.get_nowait()
                    except queue.Empty:
                        try:
                            item = self.event_queue.get(True, 0.03)
                        except queue.Empty:
                            continue

                    (event_tag, data) = item

                    if event_tag == TAG_CMD:
                        self.handle_commands(data, self.target)
                    elif event_tag == TAG_KEY:
                        try:
                            self.serial.write(codecs.encode(data))
                        except serial.SerialException:
                            pass  # this shouldn't happen, but sometimes port has closed in serial thread
                        except UnicodeEncodeError:
                            pass  # this can happen if a non-ascii character was passed, ignoring
                    elif event_tag == TAG_SERIAL:
                        self.handle_serial_input(data)
                        if self._invoke_processing_last_line_timer is not None:
                            self._invoke_processing_last_line_timer.cancel()
                        self._invoke_processing_last_line_timer = threading.Timer(0.1,
                                                                                  self.invoke_processing_last_line)
                        self._invoke_processing_last_line_timer.start()
                        # If no further data is received in the next short period
                        # of time then the _invoke_processing_last_line_timer
                        # generates an event which will result in the finishing of
                        # the last line. This is fix for handling lines sent
                        # without EOL.
                    elif event_tag == TAG_SERIAL_FLUSH:
                        self.handle_serial_input(data, finalize_line=True)
                    else:
                        raise RuntimeError('Bad event data %r' % ((event_tag, data),))
                except KeyboardInterrupt:
                    try:
                        yellow_print('To exit from IDF monitor please use \"Ctrl+]\"')
                        self.serial.write(codecs.encode('\x03'))
                    except serial.SerialException:
                        pass  # this shouldn't happen, but sometimes port has closed in serial thread
                    except UnicodeEncodeError:
                        pass  # this can happen if a non-ascii character was passed, ignoring
        except SerialStopException:
            normal_print('Stopping condition has been received\n')
        except KeyboardInterrupt:
            pass
        finally:
            try:
                self.console_reader.stop()
                self.serial_reader.stop()
                self.logger.stop_logging()
                # Cancelling _invoke_processing_last_line_timer is not
                # important here because receiving empty data doesn't matter.
                self._invoke_processing_last_line_timer = None
            except Exception:
                pass
            normal_print('\n')

    def check_gdb_stub_and_run(self, line):  # type: (bytes) -> None
        if self.gdb_helper.check_gdb_stub_trigger(line):
            with self:  # disable console control
                self.gdb_helper.run_gdb()

    def handle_serial_input(self, data, finalize_line=False):
        # type: (bytes, bool) -> None
        # Remove "+" after Continue command
        if self.start_cmd_sent is True:
            self.start_cmd_sent = False
            pos = data.find(b'+')
            if pos != -1:
                data = data[(pos + 1):]

        sp = data.split(b'\n')
        if self._last_line_part != b'':
            # add unprocessed part from previous "data" to the first line
            sp[0] = self._last_line_part + sp[0]
            self._last_line_part = b''
        if sp[-1] != b'':
            # last part is not a full line
            self._last_line_part = sp.pop()
        for line in sp:
            if line == b'':
                continue
            if self._serial_check_exit and line == self.console_parser.exit_key.encode('latin-1'):
                raise SerialStopException()
            self.check_panic_decode_trigger(line)
            with self.coredump.check(line):
                if self._force_line_print or self._line_matcher.match(line.decode(errors='ignore')):
                    self.logger.print(line + b'\n')
                    self.handle_possible_pc_address_in_line(line)
            self.check_gdb_stub_and_run(line)
            self._force_line_print = False
        # Now we have the last part (incomplete line) in _last_line_part. By
        # default we don't touch it and just wait for the arrival of the rest
        # of the line. But after some time when we didn't received it we need
        # to make a decision.
        force_print_or_matched = any((
            self._force_line_print,
            (finalize_line and self._line_matcher.match(self._last_line_part.decode(errors='ignore')))
        ))
        if self._last_line_part != b'' and force_print_or_matched:
            self._force_line_print = True
            self.logger.print(self._last_line_part)
            self.handle_possible_pc_address_in_line(self._last_line_part)
            self.check_gdb_stub_and_run(self._last_line_part)
            # It is possible that the incomplete line cuts in half the PC
            # address. A small buffer is kept and will be used the next time
            # handle_possible_pc_address_in_line is invoked to avoid this problem.
            # MATCH_PCADDR matches 10 character long addresses. Therefore, we
            # keep the last 9 characters.
            self._pc_address_buffer = self._last_line_part[-9:]
            # GDB sequence can be cut in half also. GDB sequence is 7
            # characters long, therefore, we save the last 6 characters.
            self.gdb_helper.gdb_buffer = self._last_line_part[-6:]
            self._last_line_part = b''
        # else: keeping _last_line_part and it will be processed the next time
        # handle_serial_input is invoked

    def handle_possible_pc_address_in_line(self, line):  # type: (bytes) -> None
        line = self._pc_address_buffer + line
        self._pc_address_buffer = b''
        if not self.enable_address_decoding:
            return
        for m in re.finditer(MATCH_PCADDR, line.decode(errors='ignore')):
            translation = lookup_pc_address(m.group(), self.toolchain_prefix, self.elf_file)
            if translation:
                self.logger.print(translation, console_printer=yellow_print)

    def __enter__(self):
        # type: () -> None
        """ Use 'with self' to temporarily disable monitoring behaviour """
        self.serial_reader.stop()
        self.console_reader.stop()

    def __exit__(self, *args, **kwargs):  # type: ignore
        """ Use 'with self' to temporarily disable monitoring behaviour """
        self.console_reader.start()
        self.serial_reader.gdb_exit = self.gdb_helper.gdb_exit  # write gdb_exit flag
        self.serial_reader.start()

    def run_make(self, target):  # type: (str) -> None
        with self:
            if isinstance(self.make, list):
                popen_args = self.make + [target]
            else:
                popen_args = [self.make, target]
            yellow_print('Running %s...' % ' '.join(popen_args))
            p = subprocess.Popen(popen_args, env=os.environ)
            try:
                p.wait()
            except KeyboardInterrupt:
                p.wait()
            if p.returncode != 0:
                prompt_next_action('Build failed', self.console, self.console_parser, self.event_queue,
                                   self.cmd_queue)
            else:
                self.logger.output_enabled = True

    def check_panic_decode_trigger(self, line):  # type: (bytes) -> None
        if self._decode_panic == PANIC_DECODE_DISABLE:
            return

        if self._reading_panic == PANIC_IDLE and re.search(PANIC_START, line.decode('ascii', errors='ignore')):
            self._reading_panic = PANIC_READING
            yellow_print('Stack dump detected')

        if self._reading_panic == PANIC_READING and PANIC_STACK_DUMP in line:
            self.logger.output_enabled = False

        if self._reading_panic == PANIC_READING:
            self._panic_buffer += line.replace(b'\r', b'') + b'\n'

        if self._reading_panic == PANIC_READING and PANIC_END in line:
            self._reading_panic = PANIC_IDLE
            self.logger.output_enabled = True
            self.gdb_helper.process_panic_output(self._panic_buffer, self.logger, self.target)
            self._panic_buffer = b''

    def handle_commands(self, cmd, chip):  # type: (int, str) -> None
        config = get_chip_config(chip)
        reset_delay = config['reset']
        enter_boot_set = config['enter_boot_set']
        enter_boot_unset = config['enter_boot_unset']

        high = False
        low = True

        if cmd == CMD_STOP:
            self.console_reader.stop()
            self.serial_reader.stop()
        elif cmd == CMD_RESET:
            self.serial.setRTS(low)
            self.serial.setDTR(self.serial.dtr)  # usbser.sys workaround
            time.sleep(reset_delay)
            self.serial.setRTS(high)
            self.serial.setDTR(self.serial.dtr)  # usbser.sys workaround
            self.logger.output_enabled = True
        elif cmd == CMD_MAKE:
            self.run_make('encrypted-flash' if self.encrypted else 'flash')
        elif cmd == CMD_APP_FLASH:
            self.run_make('encrypted-app-flash' if self.encrypted else 'app-flash')
        elif cmd == CMD_OUTPUT_TOGGLE:
            self.logger.output_toggle()
        elif cmd == CMD_TOGGLE_LOGGING:
            self.logger.toggle_logging()
        elif cmd == CMD_TOGGLE_TIMESTAMPS:
            self.logger.toggle_timestamps()
            self.logger.toggle_logging()
        elif cmd == CMD_ENTER_BOOT:
            self.serial.setDTR(high)  # IO0=HIGH
            self.serial.setRTS(low)  # EN=LOW, chip in reset
            self.serial.setDTR(self.serial.dtr)  # usbser.sys workaround
            time.sleep(enter_boot_set)  # timeouts taken from esptool.py, includes esp32r0 workaround. defaults: 0.1
            self.serial.setDTR(low)  # IO0=LOW
            self.serial.setRTS(high)  # EN=HIGH, chip out of reset
            self.serial.setDTR(self.serial.dtr)  # usbser.sys workaround
            time.sleep(enter_boot_unset)  # timeouts taken from esptool.py, includes esp32r0 workaround. defaults: 0.05
            self.serial.setDTR(high)  # IO0=HIGH, done
        else:
            raise RuntimeError('Bad command data %d' % cmd)  # type: ignore


def main():  # type: () -> None
    parser = argparse.ArgumentParser('idf_monitor - a serial output monitor for esp-idf')

    parser.add_argument(
        '--port', '-p',
        help='Serial port device',
        default=os.environ.get('ESPTOOL_PORT', '/dev/ttyUSB0')
    )

    parser.add_argument(
        '--disable-address-decoding', '-d',
        help="Don't print lines about decoded addresses from the application ELF file",
        action='store_true',
        default=True if os.environ.get('ESP_MONITOR_DECODE') == 0 else False
    )

    parser.add_argument(
        '--baud', '-b',
        help='Serial port baud rate',
        type=int,
        default=os.getenv('IDF_MONITOR_BAUD', os.getenv('MONITORBAUD', 115200)))

    parser.add_argument(
        '--make', '-m',
        help='Command to run make',
        type=str, default='make')

    parser.add_argument(
        '--encrypted',
        help='Use encrypted targets while running make',
        action='store_true')

    parser.add_argument(
        '--toolchain-prefix',
        help='Triplet prefix to add before cross-toolchain names',
        default=DEFAULT_TOOLCHAIN_PREFIX)

    parser.add_argument(
        '--eol',
        choices=['CR', 'LF', 'CRLF'],
        type=lambda c: c.upper(),
        help='End of line to use when sending to the serial port',
        default='CR')

    parser.add_argument(
        'elf_file', help='ELF file of application',
        type=argparse.FileType('rb'))

    parser.add_argument(
        '--print_filter',
        help='Filtering string',
        default=DEFAULT_PRINT_FILTER)

    parser.add_argument(
        '--decode-coredumps',
        choices=[COREDUMP_DECODE_INFO, COREDUMP_DECODE_DISABLE],
        default=COREDUMP_DECODE_INFO,
        help='Handling of core dumps found in serial output'
    )

    parser.add_argument(
        '--decode-panic',
        choices=[PANIC_DECODE_BACKTRACE, PANIC_DECODE_DISABLE],
        default=PANIC_DECODE_DISABLE,
        help='Handling of panic handler info found in serial output'
    )

    parser.add_argument(
        '--target',
        help='Target name (used when stack dump decoding is enabled)',
        default=os.environ.get('IDF_TARGET', 'esp32')
    )

    parser.add_argument(
        '--revision',
        help='Revision of the target',
        type=int,
        default=0
    )

    parser.add_argument(
        '--ws',
        default=os.environ.get('ESP_IDF_MONITOR_WS', None),
        help='WebSocket URL for communicating with IDE tools for debugging purposes'
    )

    parser.add_argument(
        '--timestamps',
        help='Add timestamp for each line',
        default=False,
        action='store_true')

    parser.add_argument(
        '--timestamp-format',
        default=os.environ.get('ESP_IDF_MONITOR_TIMESTAMP_FORMAT', '%Y-%m-%d %H:%M:%S'),
        help='Set a strftime()-compatible timestamp format'
    )

    args = parser.parse_args()

    # GDB uses CreateFile to open COM port, which requires the COM name to be r'\\.\COMx' if the COM
    # number is larger than 10
    if os.name == 'nt' and args.port.startswith('COM'):
        args.port = args.port.replace('COM', r'\\.\COM')
        yellow_print('--- WARNING: GDB cannot open serial ports accessed as COMx')
        yellow_print('--- Using %s instead...' % args.port)
    elif args.port.startswith('/dev/tty.') and sys.platform == 'darwin':
        args.port = args.port.replace('/dev/tty.', '/dev/cu.')
        yellow_print('--- WARNING: Serial ports accessed as /dev/tty.* will hang gdb if launched.')
        yellow_print('--- Using %s instead...' % args.port)

    serial_instance = serial.serial_for_url(args.port, args.baud,
                                            do_not_open=True)
    serial_instance.dtr = False
    serial_instance.rts = False
    args.elf_file.close()  # don't need this as a file

    # remove the parallel jobserver arguments from MAKEFLAGS, as any
    # parent make is only running 1 job (monitor), so we can re-spawn
    # all of the child makes we need (the -j argument remains part of
    # MAKEFLAGS)
    try:
        makeflags = os.environ['MAKEFLAGS']
        makeflags = re.sub(r'--jobserver[^ =]*=[0-9,]+ ?', '', makeflags)
        os.environ['MAKEFLAGS'] = makeflags
    except KeyError:
        pass  # not running a make jobserver

    # Pass the actual used port to callee of idf_monitor (e.g. make) through `ESPPORT` environment
    # variable
    # To make sure the key as well as the value are str type, by the requirements of subprocess
    espport_key = str('ESPPORT')
    espport_val = str(args.port)
    os.environ.update({espport_key: espport_val})

    ws = WebSocketClient(args.ws) if args.ws else None
    try:
        monitor = Monitor(serial_instance, args.elf_file.name, args.print_filter, args.make, args.encrypted,
                          args.toolchain_prefix, args.eol,
                          args.decode_coredumps, args.decode_panic, args.target,
                          ws, enable_address_decoding=not args.disable_address_decoding,
                          timestamps=args.timestamps, timestamp_format=args.timestamp_format)

        yellow_print('--- idf_monitor on {p.name} {p.baudrate} ---'.format(p=serial_instance))
        yellow_print('--- Quit: {} | Menu: {} | Help: {} followed by {} ---'.format(
            key_description(monitor.console_parser.exit_key),
            key_description(monitor.console_parser.menu_key),
            key_description(monitor.console_parser.menu_key),
            key_description(CTRL_H)))
        if args.print_filter != DEFAULT_PRINT_FILTER:
            yellow_print('--- Print filter: {} ---'.format(args.print_filter))

        monitor.main_loop()
    except KeyboardInterrupt:
        pass
    finally:
        if ws:
            ws.close()


if __name__ == '__main__':
    main()
