#!
# @file nc-ddns-update.py
# @brief Namecheap Dyanmic DNS utilities
#
# Namecheap offers a great DDNS service, but the software (and router integration)
# available to let Namecheap's DNS servers know when your public IP address has
# changed are not plentiful or portable.
#
# This script aims to become the defacto standard for manual and automated
# (e.g. via cron) updating of Namecheap DDNS records.
#
# @author Ryan M. Lederman <lederman@gmail.com>
# @copyright The MIT License (MIT)
#
# Permission is hereby granted, free of charge, to any person obtaining a copy of
# this software and associated documentation files (the "Software"), to deal in
# the Software without restriction, including without limitation the rights to
# use, copy, modify, merge, publish, distribute, sublicense, and/or sell copies of
# the Software, and to permit persons to whom the Software is furnished to do so,
# subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in all
# copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY, FITNESS
# FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR
# COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER
# IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN
# CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.
#

__version__ = "0.1.1b"
__script_name__ = "nc-ddns-update.py"

import requests
from requests.adapters import HTTPAdapter, Retry
from urllib3 import HTTPResponse, exceptions
import argparse
import webbrowser
import logging
import re
import typing
import inspect
import time
import os

#============================== Constants =====================================#

# The default timeouts for an HTTP GET request, in seconds (connect, read).
HTTP_TIMEOUTS = (6.05, 27.05)

# The maximim number of times to retry a failed HTTP request.
MAX_RETRIES = 20

# The maximum HTTP redirects to tolerate.
MAX_REDIRECTS = 3

# The factor used to determine the next exponential backoff interval.
BACKOFF_FACTOR = 0.5

# The amount of jitter to apply to the backoff interval.
BACKOFF_JITTER = 0.325

# The longest possible retry backoff interval, in seconds.
MAX_BACKOFF = (5.0 * 60.0)

# Namecheap DDNS API endpoint
NC_DDNS_URL = 'https://httpstat.us/429' #'https://dynamicdns.park-your-domain.com/update'

# The GitHub repository that this script was born in.
NC_DDNS_GH_REPO = 'https://github.com/aremmell/namecheap-ddns'

# The link directly to README.md
NC_DDNS_GH_README = f'{NC_DDNS_GH_REPO}/blob/main/README.md'

# The default service for resolution of public IP addresses.
IP_SERVICE = 'https://api.ipify.org'

# Whether or not to print the response body from Namecheap's server
# in the debug log. Disabled by default. Change this to 1 to enable.
PRINT_XML_RESPONSE_BODY = 0

#========================= Terminal syling ====================================#

# base ANSI escape code generation
def ansi_esc(codes: str) -> str:
    return f'\x1b[{codes}m'

# ansi escape reset
def ansi_esc_end() -> str:
    return ansi_esc('0')

# ansi escape: basic 4-bit bold/dim, foreground, background
def ansi_esc_basic(msg: str, attr: int = 0, fg: int = 39, bg: int = 49) -> str:
    return f'{ansi_esc(f"{str(attr)};{str(fg)};{str(bg)}")}{msg}{ansi_esc_end()}'

def error_msg(msg: str) -> str:
    return ansi_esc_basic(msg, 1, 31)

def success_msg(msg: str) -> str:
    return ansi_esc_basic(msg, 1, 32)

def warning_msg(msg: str) -> str:
    return ansi_esc_basic(msg, 1, 33)

#========================== Exception handling ================================#

# returns information about the frame before itself; so whatever function calls
# get_tb() will have its information recorded for printing to the log.
def get_tb() -> inspect.Traceback | None:
    outer_frames = inspect.getouterframes(inspect.currentframe())
    if not outer_frames or len(outer_frames) < 2:
        return None
    else:
        return inspect.getframeinfo(outer_frames[1].frame)

def tb_to_str(tb: inspect.Traceback | None) -> str:
    if not tb:
        return "<???>"
    else:
        return f'{tb.function} in {os.path.basename(tb.filename)}:{tb.lineno}'

def on_critical_exception(e: Exception, tb: inspect.Traceback | None):
    logging.exception(error_msg(f'{tb_to_str(tb)}: {e}'))

#=============================== Network ======================================#

# Allows for interception of transaction failures (retries), so that they
# may be logged as warnings.
class NcDdnsRetry(Retry):
    def __init__(self, *args, **kwargs):
        self._callback = kwargs.pop('callback', None)
        super(NcDdnsRetry, self).__init__(**kwargs)

    def new(self, **kw: typing.Any):
        kw['callback'] = self._callback
        return super(NcDdnsRetry, self).new(**kw)

    def increment(self, method, url, *args,**kwargs):
        try:
            if self._callback:
                self._callback(
                    self,
                    kwargs.get('response', None),
                    kwargs.get('error', None)
                )
        except Exception:
            # this exception is not important enough for the critical handler.
            logging.exception('Retry callback raised an exception; ignoring.')

        logging.debug(f'{method}, {url}, {args}, {kwargs}')
        return super(NcDdnsRetry, self).increment(method, url, *args, **kwargs)

def http_retry_callback(
        retry: NcDdnsRetry,
        response: HTTPResponse,
        err: Exception | None
    ):
    # err and response are not always set.
    if err:
        logging.warning(f'err: {err.args}')
    else:
        logging.warning("got no err")
    
    if response:
        logging.warning(f'response: url= {response.geturl()}, status:{response.status}')
        logging.warning(f'response: reason: {response.reason}')
        logging.warning(f'retry retry-after: {retry.get_retry_after(response)}')
    else:
        logging.warning("got no response")

        logging.warning(f'retry retry total {retry.total}, conn: {retry.connect}, read: {retry.read}, stat: {retry.status}')
        logging.warning(f'backoff: {retry.get_backoff_time()}')

def do_http_get_request(
        url: str,
        payload: dict[str, str] = dict(),
        headers: dict[str, str] = dict(),
        max_num_retries: int = MAX_RETRIES
    ) -> requests.Response | None:
    try:
        headers['User-Agent'] = f'{__script_name__}/{__version__}'

        session = requests.Session()
        retries = NcDdnsRetry(
            total=max_num_retries,
            connect=max_num_retries,
            read=max_num_retries,
            redirect=MAX_REDIRECTS,
            status=max_num_retries,
            other=max_num_retries,
            backoff_factor=BACKOFF_FACTOR,
            backoff_jitter=BACKOFF_JITTER,
            backoff_max=MAX_BACKOFF,
            raise_on_redirect=True,
            respect_retry_after_header=True,
            status_forcelist=frozenset({408, 413, 429, 502, 503, 504}),
            callback=http_retry_callback
        )

        session.mount('https://', HTTPAdapter(max_retries=retries))
        session.mount('http://', HTTPAdapter(max_retries=retries))        

        logging.debug(
            f'Performing GET request to \'{url}\' with params:' +
            f' \'{payload}\', headers: \'{headers}\', timeouts' +
            f' (conn, read): {HTTP_TIMEOUTS}, max retries:' +
            f' {max_num_retries}, max redirs: {MAX_REDIRECTS}...'
        )

        # this is the meat–between these counter start and end calls.
        t_start = time.perf_counter_ns()
        r = session.get(
            url,
            params=payload,
            headers=headers,
            timeout=HTTP_TIMEOUTS,
        )
        t_end = time.perf_counter_ns()

        if r.status_code != requests.codes.ok:
            r.raise_for_status()
        else:
            logging.debug(
                'Request successful (%.04fsec)' % ((t_end - t_start) / 1e9)
            )
        return r

    except requests.exceptions.RequestException as e:
        # all attempts have failed (or retries were disabled), or an HTTP error
        # code was returned that is not in the retry list (e.g. 500).
        on_critical_exception(e, get_tb())
        return None

#========================== Response parser ===================================#    

def parse_xml_response(xml_data: str):
    if PRINT_XML_RESPONSE_BODY != 0:
        logging.debug("XML response body:\n%s\n" % xml_data)

    logging.debug("Using regex to parse XML...")

    def search_xml(pattern: str, flags: re.RegexFlag) -> re.Match[str] | None:
        logging.debug("Searching regex pattern: '%s'..." % pattern)

        m = re.search(pattern, xml_data, flags)
        if m is None:
            logging.debug("No match: '%s'" % pattern)            
        else:
            logging.debug("Match: %r, groups: %r" % (m.group(), m.groups()))

        return m
    
    def findall_xml(pattern: str, flags: re.RegexFlag) -> list[typing.Any]:
        logging.debug("Searching all instances of pattern: '%s'..." % pattern)

        m = re.findall(pattern, xml_data, flags);
        if type(m) is list and len(m) > 0:
            logging.debug("Match(es): %s" % m);
        else:
            logging.debug("No match: %s" % m)
        
        return m

    try:
        re_flags = re.ASCII | re.MULTILINE
        err_patterns = [
            r'<ErrCount>(\d+)</ErrCount>',                                  # 1
            r'<Err[\d]>(.+)</Err[\d]>',                                     # 2
            r'<ResponseCount>(\d)</ResponseCount>',                         # 3
            r'<response>(?:[\s\n]+)<Description>(.+)</(?:\w+)>(?:[\s\n]+)'  # 4
            r'<ResponseNumber>(.+)</(?:\w+)>(?:[\s\n]+)<ResponseString>(.+)'# 4
            r'</(?:\w+)>(?:[\s\n]+)</(?:\w+)>'                              # 4
        ]

        success_patterns = [
            r'<ErrCount>0</ErrCount>',
            r'<ResponseCount>0</ResponseCount>',
            r'<IP>(.+)</IP>',            
        ]

        final_err_set = list(())

        # look for error count, which hints at whether or not we should
        # look for the second pattern.
        m1 = search_xml(err_patterns[0], re_flags)
        if m1: # got a match; error count = group 1
            n_err = int(m1.group(1))
            if n_err > 0: # extract the error message(s) from the second pattern.
                logging.debug(f'errors: {n_err}; looking for error messages...')                
                m2 = findall_xml(err_patterns[1], re_flags)
                if m2:
                    for i in range(len(m2)):
                        logging.debug("Found error description: %s" % m2[i])
                        final_err_set.append(m2[i])
            # look for response count, which also contains additional error
            # information, if any <response> tags are present.
            m3 = search_xml(err_patterns[2], re_flags)
            if m3: # got a match; response count = group 1
                n_resp = int(m3.group(1))
                if n_resp > 0: # find and extract <response> tag contents
                    logging.debug("responses: %d; looking for response"
                                  " content..." % n_resp)
                    m4 = findall_xml(err_patterns[3], re_flags)
                    if m4: # this should be a list of tuples, since there were
                           # 3 capture groups.
                        for i in range(len(m4)):
                            this_response = ""
                            for n in range(len(m4[i])):
                                this_response += m4[i][n]
                                if n <= 1: this_response += ": "
                            final_err_set.append(this_response);
        # if final_err_set is empty, no errors were found, and it's time
        # to move on to searching for known success patterns.
        if len(final_err_set) == 0:
            all_succeeded = True
            final_result  = ""
            for p in range(len(success_patterns)):
                m = search_xml(success_patterns[p], re_flags)
                if m:
                    logging.debug("verified %d/%d expected success patterns"
                                    %(p + 1, len(success_patterns)))
                    if p == len(success_patterns) - 1:
                        final_result = m.group(1)
                else:
                    all_succeeded = False
            if all_succeeded:
                logging.info(
                    success_msg(
                        f'Successfully updated A record with IP: {final_result}'
                    )
                );
            return all_succeeded
        else: # all done; print final list of errors and return.
            logging.error("Failed to update A record! Found these error(s) in"
                          " the response body:\n");
            for e in range(len(final_err_set)):
                logging.error("\t%d: '%s'" % (e + 1, final_err_set[e]))

            return False
    except re.error as e:
        logging.error("regex exception: %s" % e)
        return False    

#================================= CLI ========================================#    

def build_cli_parser():
    # top-level parser
    argparser = argparse.ArgumentParser(
        prog='nc-ddns-update.py',
        description='Namecheap Dynamic DNS utilities.',
        epilog=f'For updates, filing bug reports, making feature requests,' +
               f' etc., visit {NC_DDNS_GH_REPO}.'
    )

    argparser.add_argument(
        '--debug',
        help='Enables debug mode. Detailed diagnostic information will be' +
             ' printed during the execution of this script.',
        action='store_true'
    )

    subparsers = argparser.add_subparsers(
        title='Commands',
        description="Available commands",
        dest='command',
        required=True
    )

    # update command
    sp_update = subparsers.add_parser(
        name='update',
        help='Updates the A record for the specified Namecheap DDNS domain.'
    )

    sp_update.add_argument(
        '-d',
        '--domain',
        help='The TLD (top-level domain) to update the A record for.' +
             ' Note: this field is case-sensitive. It must be entered exactly' +
             ' as it appears in your Namecheap account.',
        required=True,
        type=str,
        metavar='domain'
    )

    sp_update.add_argument(
        '-p',
        '--password',
        help='Your Namecheap DDNS password. This is *not* the same as your' +
             ' Namecheap account password.' +
             '' +
             ' Locatte your DDNS password: \'Domain List\' -> (your domain) ->' +
             ' \'Manage\', -> \'Domain\' drop-down -> \'Advanced DNS\'' +
             ' Scroll down to \'Dynamic DNS.\'',
        required=True,
        type=str,
        metavar='pw'
    )

    sp_update.add_argument(
        '-i',
        '--ip',
        help='The IPv4 address to update the A record with.' +
             ' If omitted, Namecheap will use your client address.' +
             ' You may also use the `resolve` command in this script' +
             ' and a third-party service will be used to determine the' +
             ' address.',
        required=False,
        type=str,
        default=None,
        metavar='addr'
    )

    rt_group = sp_update.add_mutually_exclusive_group()

    class IntGreaterThanZeroAction(argparse.Action):

        def __call__(self, parser, namespace, values, option_string=None):
            if not values > 0:
                parser.error(f'{option_string} must be greater than zero.')

            setattr(namespace, self.dest, values)

    rt_group.add_argument(
        '-r',
        '--retry',
        help='Retry failed network transactions when circumstances allow.' +
             ' This is the default setting. Retries will be performed' +
            f' {MAX_RETRIES} times, or until a non-retryable error is' +
             ' encountered. An exponential backoff algorithm is used to' +
             ' calculate the interval between retries. For further'
             ' information, see `--docs`.',
        action=IntGreaterThanZeroAction,
        type=int,
        default=MAX_RETRIES,
        metavar='num'
    )

    rt_group.add_argument(
        '-nr',
        '--no-retry',
        help='Do not retry failed network transactions, but instead exit with' +
             ' an error.',
        action='store_true',
    )

    # resolve command
    sp_resolve = subparsers.add_parser(
        name='resolve',
        help='Resolves your public IP address using a third-party service' +
             ' and prints it to stdout.',
    )

    sp_resolve.add_argument(
        '-s',
        '--service',
        help='If specified, override the third-party service used to resolve' +
            f' your public IP address. (default: {IP_SERVICE})'
             ' Note: the service must return a plaintext response containing only' +
             ' the IPv4 address. Currently, that is the only response supported.',
        required=False,             
        type=str,
        default=None,
        metavar='url'
    )

    # docs command
    sp_docs = subparsers.add_parser(
        name='docs',
        help='Obtain more information about this script, how to use it, and how' +
             ' you can expect it to behave.'
    )

    docs_group = sp_docs.add_mutually_exclusive_group(
        required=True
    )

    docs_group.add_argument(
        '-o',
        '--online',
        help='Opens the online documentation in your default web browser.',
        action='store_true'
    )

    docs_group.add_argument(
        '-p',
        '--print',
        help='Print unformatted, limited documentation in the terminal.',
        action='store_true'
    )

    return argparser
    
# entry point for the 'update' command
def do_update_request(arg_ns: argparse.Namespace):
    payload = dict(host = '@', domain = arg_ns.domain, password = arg_ns.password)
    if (arg_ns.ip is not None):
        payload['ip'] = arg_ns.ip

    max_retries = arg_ns.retry if not arg_ns.no_retry else 0
    response = do_http_get_request(NC_DDNS_URL, payload, dict(), max_retries)
    if response is None:
        logging.error(error_msg('Failed to update A record!'))
        return False
    else:
        return parse_xml_response(response.text)

# entry point for the 'resolve' command
def do_resolve_request(arg_ns: argparse.Namespace):
    svc = IP_SERVICE if arg_ns.service is None else arg_ns.service
    response = do_http_get_request(svc)
    if response is None:
        logging.error(error_msg('Failed to resolve your public IP address!'))
        return False
    else:
        try:
            ip_v4_pattern = r'^[\d]{1,3}\.[\d]{1,3}\.[\d]{1,3}\.[\d]{1,3}$'
            m = re.fullmatch(ip_v4_pattern, response.text, re.A)
            if m:
                logging.info(
                    success_msg(
                        f'Success! Your public IP address is: {response.text}'
                    )
                )
                return True
            else:
                logging.error(response.text);
                logging.error(
                    error_msg(
                        f'The response from {svc} isn\'t an IPv4 address!'
                    )
                )
                return False
        except re.error as e:
            on_critical_exception(e, get_tb())
            return False

# entry point for the 'docs' command
def do_display_docs(arg_ns: argparse.Namespace) -> bool:
    if arg_ns.print:
        print("TODO: print limited documentation here; perhaps from an online file.")
        return True
    elif arg_ns.online:
        return webbrowser.open(NC_DDNS_GH_README)
    else:
        return False
    
# script entry point
if __name__ == "__main__":
    try:
        argparser = build_cli_parser()
        arg_ns = argparser.parse_args()

        logging.basicConfig(format='%(asctime)s %(levelname)s %(message)s',
                            level=logging.DEBUG if arg_ns.debug else logging.INFO)

        if arg_ns.debug:
            logging.debug("Debug logging enabled.")
            logging.debug("argparse NS: %s" % arg_ns)

        logging.info("Executing command: '%s'..." % arg_ns.command)

        if arg_ns.command == 'resolve':
            exit_code = 0 if do_resolve_request(arg_ns) else 1
        elif arg_ns.command == 'update':
            exit_code = 0 if do_update_request(arg_ns) else 1
        elif arg_ns.command == 'docs':
            exit_code = 0 if do_display_docs(arg_ns) else 1
        else:
            logging.error("Unknown command: %s" % arg_ns.command)
            exit_code = 1        
    except Exception as e:
        logging.critical("Exception in __main__: %s" % e)
        assert()
        exit_code = 1

    logging.debug("Exiting with code: %d" % exit_code)
    exit(exit_code)
