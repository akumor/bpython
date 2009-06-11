#!/usr/bin/env python
#
# The MIT License
#
# Copyright (c) 2008 Bob Farrell
#
# Permission is hereby granted, free of charge, to any person obtaining a copy
# of this software and associated documentation files (the "Software"), to deal
# in the Software without restriction, including without limitation the rights
# to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
# copies of the Software, and to permit persons to whom the Software is
# furnished to do so, subject to the following conditions:
#
# The above copyright notice and this permission notice shall be included in
# all copies or substantial portions of the Software.
#
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
# IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
# FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
# AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
# LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
# OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
# THE SOFTWARE.
#

from __future__ import with_statement

import codecs
import os
import sys
import curses
import code
import traceback
import re
import time
import urllib
import rlcompleter
import inspect
import signal
import struct
import termios
import fcntl
import string
import socket
import pydoc
import types
import unicodedata
from cStringIO import StringIO
from locale import LC_ALL, getpreferredencoding, setlocale
from optparse import OptionParser
from urlparse import urljoin
from xmlrpclib import ServerProxy, Error as XMLRPCError
from ConfigParser import ConfigParser, NoSectionError, NoOptionError

# These are used for syntax hilighting.
from pygments import format
from pygments.lexers import PythonLexer
from pygments.token import Token
from bpython.formatter import BPythonFormatter, Parenthesis

# This for import completion
from bpython import importcompletion

# This for config
from bpython.config import Struct, loadini, migrate_rc

# This for keys
from bpython.keys import key_dispatch

from bpython import __version__

def log(x):
    f = open('/tmp/bpython.log', 'a')
    f.write('%s\n' % (x,))

orig_stdout = sys.__stdout__
stdscr = None


class FakeStdin(object):
    """Provide a fake stdin type for things like raw_input() etc."""

    def __init__(self, interface):
        """Take the curses Repl on init and assume it provides a get_key method
        which, fortunately, it does."""

        self.encoding = getpreferredencoding()
        self.interface = interface

    def readline(self):
        """I can't think of any reason why anything other than readline would
        be useful in the context of an interactive interpreter so this is the
        only one I've done anything with. The others are just there in case
        someone does something weird to stop it from blowing up."""

        buffer = ''
        while True:
            key = self.interface.get_key()
            sys.stdout.write(key)
# Include the \n in the buffer - raw_input() seems to deal with trailing
# linebreaks and will break if it gets an empty string.
            buffer += key
            if key == '\n':
                break

        return buffer

    def read(self, x):
        pass

    def readlines(self, x):
        pass

OPTS = Struct()
DO_RESIZE = False

# TODO:
#
# Tab completion does not work if not at the end of the line.
#
# Numerous optimisations can be made but it seems to do all the lookup stuff
# fast enough on even my crappy server so I'm not too bothered about that
# at the moment.
#
# The popup window that displays the argspecs and completion suggestions
# needs to be an instance of a ListWin class or something so I can wrap
# the addstr stuff to a higher level.
#


def DEBUG(s):
    """This shouldn't ever be called in any release of bpython, so
    beat me up if you find anything calling it."""
    open('/tmp/bpython-debug', 'a').write("%s\n" % (str(s), ))


def get_color(name):
    return colors[OPTS.color_scheme[name].lower()]

def get_colpair(name):
    return curses.color_pair(get_color(name) + 1)

def make_colors():
    """Init all the colours in curses and bang them into a dictionary"""

    # blacK, Red, Green, Yellow, Blue, Magenta, Cyan, White, Default:
    c = {
        'k' : 0,
        'r' : 1,
        'g' : 2,
        'y' : 3,
        'b' : 4,
        'm' : 5,
        'c' : 6,
        'w' : 7,
        'd' : -1,
    }
    for i in range(63):
        if i > 7:
            j = i / 8
        else:
            j = c[OPTS.color_scheme['background']]
        curses.init_pair(i+1, i % 8, j)

    return c


def next_token_inside_string(s, inside_string):
    """Given a code string s and an initial state inside_string, return
    whether the next token will be inside a string or not."""
    for token, value in PythonLexer().get_tokens(s):
        if token is Token.String:
            value = value.lstrip('bBrRuU')
            if value in ['"""', "'''", '"', "'"]:
                if not inside_string:
                    inside_string = value
                elif value == inside_string:
                    inside_string = False
    return inside_string


class Interpreter(code.InteractiveInterpreter):

    def __init__(self):
        """The syntaxerror callback can be set at any time and will be called
        on a caught syntax error. The purpose for this in bpython is so that
        the repl can be instantiated after the interpreter (which it
        necessarily must be with the current factoring) and then an exception
        callback can be added to the Interpeter instance afterwards - more
        specifically, this is so that autoindentation does not occur after a
        traceback.

        Interpreter.tblist_hook can be a function that will receive the tblist
        from showtraceback() (after it has been modified to retain only the
        last element) as the only argument and should mutate it in place."""

        self.syntaxerror_callback = None
        self.tblist_hook = None
# Unfortunately code.InteractiveInterpreter is a classic class, so no super()
        code.InteractiveInterpreter.__init__(self)

    def showsyntaxerror(self, filename=None):
        """Override the regular handler, the code's copied and pasted from
        code.py, as per showtraceback, but with the syntaxerror callback called
        and the text in a pretty colour."""
        if self.syntaxerror_callback is not None:
            self.syntaxerror_callback()

        type, value, sys.last_traceback = sys.exc_info()
        sys.last_type = type
        sys.last_value = value
        if filename and type is SyntaxError:
            # Work hard to stuff the correct filename in the exception
            try:
                msg, (dummy_filename, lineno, offset, line) = value
            except:
                # Not the format we expect; leave it alone
                pass
            else:
                # Stuff in the right filename
                value = SyntaxError(msg, (filename, lineno, offset, line))
                sys.last_value = value
        list = traceback.format_exception_only(type, value)
        self.writetb(list)

    def showtraceback(self):
        """This needs to override the default traceback thing
        so it can put it into a pretty colour and maybe other
        stuff, I don't know"""
        try:
            t, v, tb = sys.exc_info()
            sys.last_type = t
            sys.last_value = v
            sys.last_traceback = tb
            tblist = traceback.extract_tb(tb)
            del tblist[:1]
            if self.tblist_hook is not None:
                self.tblist_hook(tblist)

            l = traceback.format_list(tblist)
            if l:
                l.insert(0, "Traceback (most recent call last):\n")
            l[len(l):] = traceback.format_exception_only(t, v)
        finally:
            tblist = tb = None

        self.writetb(l)

    def writetb(self, l):
        """This outputs the traceback and should be overridden for anything
        fancy."""
        map(self.write, ["\x01%s\x03%s" % (OPTS.color_scheme['error'], i) for i in l])


class Repl(object):
    """Implements the necessary guff for a Python-repl-alike interface

    The execution of the code entered and all that stuff was taken from the
    Python code module, I had to copy it instead of inheriting it, I can't
    remember why. The rest of the stuff is basically what makes it fancy.

    It reads what you type, passes it to a lexer and highlighter which
    returns a formatted string. This then gets passed to echo() which
    parses that string and prints to the curses screen in appropriate
    colours and/or bold attribute.

    The Repl class also keeps two stacks of lines that the user has typed in:
    One to be used for the undo feature. I am not happy with the way this
    works.  The only way I have been able to think of is to keep the code
    that's been typed in in memory and re-evaluate it in its entirety for each
    "undo" operation. Obviously this means some operations could be extremely
    slow.  I'm not even by any means certain that this truly represents a
    genuine "undo" implementation, but it does seem to be generally pretty
    effective.

    If anyone has any suggestions for how this could be improved, I'd be happy
    to hear them and implement it/accept a patch. I researched a bit into the
    idea of keeping the entire Python state in memory, but this really seems
    very difficult (I believe it may actually be impossible to work) and has
    its own problems too.

    The other stack is for keeping a history for pressing the up/down keys
    to go back and forth between lines.
    """#TODO: Split the class up a bit so the curses stuff isn't so integrated.
    """

    """

    def __init__(self, scr, interp, statusbar=None, idle=None):
        """Initialise the repl with, unfortunately, a curses screen passed to
        it.  This needs to be split up so the curses crap isn't in here.

        interp is a Python code.InteractiveInterpreter instance

        The optional 'idle' parameter is a function that the repl call while
        it's blocking (waiting for keypresses). This, again, should be in a
        different class"""

        self.cut_buffer = ''
        self.buffer = []
        self.scr = scr
        self.interp = interp
        self.match = False
        self.rl_hist = []
        self.stdout_hist = ''
        self.s_hist = []
        self.history = []
        self.h_i = 0
        self.in_hist = False
        self.evaluating = False
        self.do_exit = False
        self.cpos = 0
# Use the interpreter's namespace only for the readline stuff:
        self.completer = rlcompleter.Completer(self.interp.locals)
        self.completer.attr_matches = self.attr_matches
        # Gna, Py 2.6's rlcompleter searches for __call__ inside the
        # instance instead of the type, so we monkeypatch to prevent
        # side-effects (__getattr__/__getattribute__)
        self.completer._callable_postfix = self._callable_postfix
        self.statusbar = statusbar
        self.list_win = newwin(1, 1, 1, 1)
        self.idle = idle
        self.f_string = ''
        self.matches = []
        self.argspec = None
        self.s = ''
        self.inside_string = False
        self.highlighted_paren = None
        self.list_win_visible = False
        self._C = {}
        sys.stdin = FakeStdin(self)
        self.paste_mode = False
        self.last_key_press = time.time()
        self.paste_time = 0.02
        sys.path.insert(0, '.')

        self.interp.tblist_hook = self.fix_traceback_offset

        if not OPTS.arg_spec:
            return

        pythonhist = os.path.expanduser('~/.pythonhist')
        if os.path.exists(pythonhist):
            with codecs.open(pythonhist, 'r', getpreferredencoding(),
                             'ignore') as hfile:
                self.rl_hist = hfile.readlines()

    def fix_traceback_offset(self, tblist):
        """Will be assigned to interpreter.tblist_hook and, if the interpreter
        supports it, will be called when the tblist is created and modified to
        contain only the last value. This is basically a little hack to fix the
        line number offset for the traceback due to us inserting the encoding
        header into the interpreter."""
        tblist[0] = (tblist[0][0], 1) + tblist[0][2:]

    def clean_object(self, obj):
        """Try to make an object not exhibit side-effects on attribute
        lookup. Return the type's magic attributes so they can be reapplied
        with restore_object"""
        type_ = type(obj)
        __getattribute__ = None
        __getattr__ = None
        # Dark magic:
        # If __getattribute__ doesn't exist on the class and __getattr__ does
        # then __getattr__ will be called when doing
        #   getattr(type_, '__getattribute__', None)
        # so we need to first remove the __getattr__, then the
        # __getattribute__, then look up the attributes and then restore the
        # original methods. :-(
        # The upshot being that introspecting on an object to display its
        # attributes will avoid unwanted side-effects.
        if type_ != types.InstanceType:
            __getattr__ = getattr(type_, '__getattr__', None)
            if __getattr__ is not None:
                try:
                    setattr(type_, '__getattr__', (lambda _: None))
                except TypeError:
                    __getattr__ = None
            __getattribute__ = getattr(type_, '__getattribute__', None)
            if __getattribute__ is not None:
                try:
                    setattr(type_, '__getattribute__', object.__getattribute__)
                except TypeError:
                    # XXX: This happens for e.g. built-in types
                    __getattribute__ = None
        # /Dark magic
        return __getattribute__, __getattr__

    def restore_object(self, obj, attribs):
        """Restore an object's magic methods as returned from clean_object"""
        type_ = type(obj)
        __getattribute__, __getattr__ = attribs
        # Dark magic:
        if __getattribute__ is not None:
            setattr(type_, '__getattribute__', __getattribute__)
        if __getattr__ is not None:
            setattr(type_, '__getattr__', __getattr__)
        # /Dark magic

    def attr_matches(self, text):
        """Taken from rlcompleter.py and bent to my will."""

        m = re.match(r"(\w+(\.\w+)*)\.(\w*)", text)
        if not m:
            return []

        expr, attr = m.group(1, 3)
        obj = eval(expr, self.interp.locals)
        attribs = self.clean_object(obj)
        try:
            matches = self.attr_lookup(obj, expr, attr)
        finally:
            self.restore_object(obj, attribs)
        return matches

    def attr_lookup(self, obj, expr, attr):
        """Second half of original attr_matches method factored out so it can
        be wrapped in a safe try/finally block in case anything bad happens to
        restore the original __getattribute__ method."""
        words = dir(obj)
        if hasattr(obj, '__class__'):
            words.append('__class__')
            words = words + rlcompleter.get_class_members(obj.__class__)

        matches = []
        n = len(attr)
        for word in words:
            if word[:n] == attr and word != "__builtins__":
                matches.append("%s.%s" % (expr, word))
        return matches

    def _callable_postfix(self, value, word):
        """rlcompleter's _callable_postfix done right."""
        attribs = self.clean_object(value)
        try:
            if hasattr(value, '__call__'):
                word += '('
        finally:
            self.restore_object(value, attribs)
        return word

    def cw(self):
        """Return the current word, i.e. the (incomplete) word directly to the
        left of the cursor"""

        if self.cpos:
# I don't know if autocomplete should be disabled if the cursor
# isn't at the end of the line, but that's what this does for now.
            return

        l = len(self.s)

        if (not self.s or
            (not self.s[l-1].isalnum() and
             self.s[l-1] not in ('.', '_'))):
            return

        i = 1
        while i < l+1:
            if not self.s[-i].isalnum() and self.s[-i] not in ('.', '_'):
                break
            i += 1
        return self.s[-i +1:]

    def get_args(self):
        """Check if an unclosed parenthesis exists, then attempt to get the
        argspec() for it. On success, update self.argspec and return True,
        otherwise set self.argspec to None and return False"""

        def getpydocspec(f, func):
            try:
                argspec = pydoc.getdoc(f)
            except NameError:
                return None

            rx = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*?)\((.*?)\)')
            s = rx.search(argspec)
            if s is None:
                return None

            if s.groups()[0] != func:
                return None

            args = [i.strip() for i in s.groups()[1].split(',')]
            return [func, (args, None, None, None)]

        def getargspec(func):
            try:
                if func in self.interp.locals:
                    f = self.interp.locals[func]
            except TypeError:
                return None

            else:
                try:
                    f = eval(func, self.interp.locals)
                except Exception:
# Same deal with the exceptions :(
                    return None

            is_bound_method = inspect.ismethod(f) and f.im_self is not None
            try:
                if inspect.isclass(f):
                    argspec = inspect.getargspec(f.__init__)
                    is_bound_method = True
                else:
                    argspec = inspect.getargspec(f)
                self.argspec = [func, argspec, is_bound_method]
                return True

            except (NameError, TypeError, KeyError):
                t = getpydocspec(f, func)
                if t is None:
                    return None
                self.argspec = t
                self.argspec.append(is_bound_method)
                return True
            except AttributeError:
# This happens if no __init__ is found
                return None

        if not OPTS.arg_spec:
            return False

        stack = [['', 0, '']]
        try:
            for (token, value) in PythonLexer().get_tokens(self.s):
                if token is Token.Punctuation:
                    if value in '([{':
                        stack.append(['', 0, value])
                    elif value in ')]}':
                        stack.pop()
                    elif value == ',':
                        try:
                            stack[-1][1] += 1
                        except TypeError:
                            stack[-1][1] = ''
                elif (token is Token.Name or token in Token.Name.subtypes or
                      token is Token.Operator and value == '.'):
                    stack[-1][0] += value
                elif token is Token.Operator and value == '=':
                    stack[-1][1] = stack[-1][0]
                else:
                    stack[-1][0] = ''
            while stack[-1][2] in '[{':
                stack.pop()
            _, arg_number, _ = stack.pop()
            func, _, _ = stack.pop()
        except IndexError:
            return False

        if getargspec(func):
            self.argspec.append(arg_number)
            return True
        return False

    def check(self):
        """Check if paste mode should still be active and, if not, deactivate
        it and force syntax highlighting."""

        if (self.paste_mode
            and time.time() - self.last_key_press > self.paste_time):
            self.paste_mode = False
            self.print_line(self.s)

    def complete(self, tab=False):
        """Wrap the _complete method to determine the visibility of list_win
        since there can be several reasons why it won't be displayed; this
        makes it more manageable."""

        if self.paste_mode and self.list_win_visible:
            self.scr.touchwin()

        if self.paste_mode:
            return

        if self.list_win_visible and not OPTS.auto_display_list:
            self.scr.touchwin()
            self.list_win_visible = False
            return

        if OPTS.auto_display_list or tab:
            self.list_win_visible = self._complete(tab)
            return

    def _complete(self, unused_tab=False):
        """Construct a full list of possible completions and construct and
        display them in a window. Also check if there's an available argspec
        (via the inspect module) and bang that on top of the completions too.
        The return value is whether the list_win is visible or not."""

        if not self.get_args():
            self.argspec = None

        cw = self.cw()
        if not (cw or self.argspec):
            self.scr.redrawwin()
            self.scr.refresh()
            return False

        if not cw:
            self.matches = []

        # Check for import completion
        e = False
        matches = importcompletion.complete(self.s, cw)
        if matches is None:
            self.scr.redrawwin()
            return False

        if not matches:
            # Nope, no import, continue with normal completion
            try:
                self.completer.complete(cw, 0)
            except Exception:
# This sucks, but it's either that or list all the exceptions that could
# possibly be raised here, so if anyone wants to do that, feel free to send me
# a patch. XXX: Make sure you raise here if you're debugging the completion
# stuff !
                e = True
            else:
                matches = self.completer.matches

        if e or not matches:
            self.matches = []
            if not self.argspec:
                self.scr.redrawwin()
                return False

        if not e and matches:
# remove duplicates and restore order
            self.matches = sorted(set(matches))

        if len(self.matches) == 1 and not OPTS.auto_display_list:
            self.list_win_visible = True
            self.tab()
            return False

        self.show_list(self.matches, self.argspec)
        return True

    def show_list(self, items, topline=None):
        shared = Struct()
        shared.cols = 0
        shared.rows = 0
        shared.wl = 0
        y, x = self.scr.getyx()
        h, w = self.scr.getmaxyx()
        down = (y < h / 2)
        if down:
            max_h = h - y
        else:
            max_h = y+1
        max_w = int(w * 0.8)

        self.list_win.erase()
        if items and '.' in items[0]:
            items = [x.rsplit('.')[-1] for x in items]

        if topline:
            height_offset = self.mkargspec(topline, down) + 1
        else:
            height_offset = 0

        def lsize():
            wl = max(len(i) for i in v_items) + 1
            if not wl:
                wl = 1
            cols = ((max_w - 2) / wl) or 1
            rows = len(v_items) / cols

            if cols * rows < len(v_items):
                rows += 1

            if rows + 2 >= max_h:
                rows = max_h - 2
                return False

            shared.rows = rows
            shared.cols = cols
            shared.wl = wl
            return True

        if items:
# visible items (we'll append until we can't fit any more in)
            v_items = [items[0][:max_w-3]]
            lsize()
        else:
            v_items = []

        for i in items[1:]:
            v_items.append(i[:max_w-3])
            if not lsize():
                del v_items[-1]
                v_items[-1] = '...'
                break

        rows = shared.rows
        if rows + height_offset < max_h:
            rows += height_offset
            display_rows = rows
        else:
            display_rows = rows + height_offset

        cols = shared.cols
        wl = shared.wl

        if topline and not v_items:
            w = max_w
        elif wl + 3 > max_w:
            w = max_w
        else:
            t = (cols + 1) * wl + 3
            if t > max_w:
                t = max_w
            w = t


        if height_offset and display_rows+5 >= max_h:
            del v_items[-(cols * (height_offset)):]

        self.list_win.resize(rows+2, w)

        if down:
            self.list_win.mvwin(y+1, 0)
        else:
            self.list_win.mvwin(y-rows-2, 0)

        if v_items:
            self.list_win.addstr('\n ')


        for ix, i in enumerate(v_items):
            padding = (wl - len(i)) * ' '
            self.list_win.addstr(i + padding, get_colpair('main'))
            if ((cols == 1 or (ix and not (ix+1) % cols))
                    and ix + 1 < len(v_items)):
                self.list_win.addstr('\n ')

# XXX: After all the trouble I had with sizing the list box (I'm not very good
# at that type of thing) I decided to do this bit of tidying up here just to
# make sure there's no unnececessary blank lines, it makes things look nicer.

        y = self.list_win.getyx()[0]
        self.list_win.resize(y + 2, w)

        self.statusbar.win.touchwin()
        self.statusbar.win.noutrefresh()
        self.list_win.attron(get_colpair('main'))
        self.list_win.border()
        self.scr.touchwin()
        self.scr.cursyncup()
        self.scr.noutrefresh()

# This looks a little odd, but I can't figure a better way to stick the cursor
# back where it belongs (refreshing the window hides the list_win)

        self.scr.move(*self.scr.getyx())
        self.list_win.refresh()

    def mkargspec(self, topline, down):
        """This figures out what to do with the argspec and puts it nicely into
        the list window. It returns the number of lines used to display the
        argspec.  It's also kind of messy due to it having to call so many
        addstr() to get the colouring right, but it seems to be pretty
        sturdy."""

        r = 3
        fn = topline[0]
        args = topline[1][0]
        kwargs = topline[1][3]
        _args = topline[1][1]
        _kwargs = topline[1][2]
        is_bound_method = topline[2]
        in_arg = topline[3]
        max_w = int(self.scr.getmaxyx()[1] * 0.6)
        self.list_win.erase()
        self.list_win.resize(3, max_w)
        h, w = self.list_win.getmaxyx()

        self.list_win.addstr('\n  ')
        self.list_win.addstr(fn,
            get_colpair('name') | curses.A_BOLD)
        self.list_win.addstr(': (', get_colpair('name'))
        maxh = self.scr.getmaxyx()[0]

        if is_bound_method and isinstance(in_arg, int):
            in_arg += 1

        for k, i in enumerate(args):
            y, x = self.list_win.getyx()
            ln = len(str(i))
            kw = None
            if kwargs and k+1 > len(args) - len(kwargs):
                kw = str(kwargs[k - (len(args) - len(kwargs))])
                ln += len(kw) + 1

            if ln + x >= w:
                ty = self.list_win.getbegyx()[0]
                if not down and ty > 0:
                    h +=1
                    self.list_win.mvwin(ty-1, 1)
                    self.list_win.resize(h, w)
                elif down and h + r < maxh-ty:
                    h += 1
                    self.list_win.resize(h, w)
                else:
                    break
                r += 1
                self.list_win.addstr('\n\t')

            if str(i) == 'self' and k == 0:
                color = get_colpair('name')
            else:
                color = get_colpair('token')

            if k == in_arg or i == in_arg:
                color |= curses.A_BOLD

            self.list_win.addstr(str(i), color)
            if kw:
                self.list_win.addstr('=', get_colpair('punctuation'))
                self.list_win.addstr(kw, get_colpair('token'))
            if k != len(args) -1:
                self.list_win.addstr(', ', get_colpair("punctuation"))

        if _args:
            if args:
                self.list_win.addstr(', ', get_colpair('punctuation'))
            self.list_win.addstr('*%s' % (_args, ), get_colpair('token'))

        if _kwargs:
            if args or _args:
                self.list_win.addstr(', ', get_colpair('punctuation'))
            self.list_win.addstr('**%s' % (_kwargs, ), get_colpair('token'))
        self.list_win.addstr(')', get_colpair('punctuation'))

        return r

    def getstdout(self):
        """This method returns the 'spoofed' stdout buffer, for writing to a
        file or sending to a pastebin or whatever."""

        return self.stdout_hist + '\n'

    def formatforfile(self, s):
        """Format the stdout buffer to something suitable for writing to disk,
        i.e. without >>> and ... at input lines and with "# OUT: " prepended to
        output lines."""

        def process():
            for line in s.split('\n'):
                if line.startswith('>>>') or line.startswith('...'):
                    yield line[4:]
                elif line.rstrip():
                    yield "# OUT: %s" % (line,)
        return "\n".join(process())

    def write2file(self):
        """Prompt for a filename and write the current contents of the stdout
        buffer to disk."""

        try:
            fn = self.statusbar.prompt('Save to file (Esc to cancel): ')
        except ValueError:
            self.statusbar.message("Save cancelled.")
            return

        if fn.startswith('~'):
            fn = os.path.expanduser(fn)

        s = self.formatforfile(self.getstdout())

        try:
            f = open(fn, 'w')
            f.write(s)
            f.close()
        except IOError:
            self.statusbar.message("Disk write error for file '%s'." % (fn, ))
        else:
            self.statusbar.message('Saved to %s' % (fn, ))

    def pastebin(self):
        """Upload to a pastebin and display the URL in the status bar."""

        pasteservice_url = 'http://paste.pocoo.org/'
        pasteservice = ServerProxy(urljoin(pasteservice_url, '/xmlrpc/'))

        s = self.getstdout()

        self.statusbar.message('Posting data to pastebin...')
        try:
            paste_id = pasteservice.pastes.newPaste('pycon', s)
        except XMLRPCError, e:
            self.statusbar.message( 'Upload failed: %s' % (str(e), ) )
            return

        paste_url = urljoin(pasteservice_url, '/show/%s/' % (paste_id, ))
        self.statusbar.message('Pastebin URL: %s' % (paste_url, ), 10)

    def make_list(self, items):
        """Compile a list of items. At the moment this simply returns
        the list; it's here in case I decide to add any more functionality.
        I originally had this method return a list of items where each item
        was prepended with a number/letter so the user could choose an option
        but it doesn't seem appropriate for readline-like behaviour."""
        return items

    def push(self, s):
        """Push a line of code onto the buffer so it can process it all
        at once when a code block ends"""
        s = s.rstrip('\n')
        self.buffer.append(s)

        try:
            encoding = getpreferredencoding()
            source = '# coding: %s\n' % (encoding, )
            source += '\n'.join(self.buffer).encode(encoding)
            more = self.interp.runsource(source)
        except SystemExit:
            # Avoid a traceback on e.g. quit()
            self.do_exit = True
            return False

        if not more:
            self.buffer = []

        return more

    def undo(self, n=1):
        """Go back in the undo history n steps and call reeavluate()
        Note that in the program this is called "Rewind" because I
        want it to be clear that this is by no means a true undo
        implementation, it is merely a convenience bonus."""
        if not self.history:
            return None

        if len(self.history) < n:
            n = len(self.history)

        self.history = self.history[:-n]
        self.reevaluate()

    def enter_hist(self):
        """Set flags for entering into the history by pressing up/down"""
        if not self.in_hist:
            self.in_hist = True
            self.ts = self.s

    def back(self):
        """Replace the active line with previous line in history and
        increment the index to keep track"""

        if not self.rl_hist:
            return None

        self.cpos = 0
        self.enter_hist()

        if self.h_i < len(self.rl_hist):
            self.h_i += 1

        self.s = self.rl_hist[-self.h_i].rstrip('\n')
        self.print_line(self.s, clr=True)

    def fwd(self):
        """Same as back() but, well, forward"""

        self.enter_hist()

        self.cpos = 0

        if self.h_i > 1:
            self.h_i -= 1
            self.s = self.rl_hist[-self.h_i].rstrip('\n')
        else:
            self.h_i = 0
            self.s = self.ts
            self.ts = ''
            self.in_hist = False

        self.print_line(self.s, clr=True)

    def redraw(self):
        """Redraw the screen."""
        self.scr.erase()
        for k, s in enumerate(self.s_hist):
            if not s:
                continue
            self.iy, self.ix = self.scr.getyx()
            for i in s.split('\x04'):
                self.echo(i, redraw=False)
            if k < len(self.s_hist) -1:
                self.scr.addstr('\n')
        self.iy, self.ix = self.scr.getyx()
        self.print_line(self.s)
        self.scr.refresh()
        self.statusbar.refresh()

    def reevaluate(self):
        """Clear the buffer, redraw the screen and re-evaluate the history"""

        self.evaluating = True
        self.stdout_hist = ''
        self.f_string = ''
        self.buffer = []
        self.scr.erase()
        self.s_hist = []

        self.prompt(False)

        self.iy, self.ix = self.scr.getyx()
        for line in self.history:
            self.stdout_hist += line.encode(getpreferredencoding()) + '\n'
            self.print_line(line)
            self.s_hist[-1] += self.f_string
# I decided it was easier to just do this manually
# than to make the print_line and history stuff more flexible.
            self.scr.addstr('\n')
            more = self.push(line)
            self.prompt(more)
            self.iy, self.ix = self.scr.getyx()

        self.s = ''
        self.scr.refresh()

        self.evaluating = False
        #map(self.push, self.history)
        #^-- That's how simple this method was at first :(

    def prompt(self, more):
        """Show the appropriate Python prompt"""
        if not more:
            self.echo("\x01%s\x03>>> " % (OPTS.color_scheme['prompt'],))
            self.stdout_hist += '>>> '
            self.s_hist.append('\x01%s\x03>>> \x04' % (OPTS.color_scheme['prompt'],))
        else:
            self.echo("\x01%s\x03... " % (OPTS.color_scheme['prompt_more'],))
            self.stdout_hist += '... '
            self.s_hist.append('\x01%s\x03... \x04' %
                (OPTS.color_scheme['prompt_more'],))

    def repl(self):
        """Initialise the repl and jump into the loop. This method also has to
        keep a stack of lines entered for the horrible "undo" feature. It also
        tracks everything that would normally go to stdout in the normal Python
        interpreter so it can quickly write it to stdout on exit after
        curses.endwin(), as well as a history of lines entered for using
        up/down to go back and forth (which has to be separate to the
        evaluation history, which will be truncated when undoing."""

# This was a feature request to have the PYTHONSTARTUP
# file executed on startup - I personally don't use this
# feature so please notify me of any breakage.
        filename = os.environ.get('PYTHONSTARTUP')
        if filename and os.path.isfile(filename):
            f = open(filename, 'r')
            code_obj = compile(f.read(), filename, 'exec')
            f.close()
            self.interp.runcode(code_obj)

# Use our own helper function because Python's will use real stdin and
# stdout instead of our wrapped
        self.push('from bpython import _internal\n')
        self.push('help = _internal._help')
        self.push('del _internal')

        self.iy, self.ix = self.scr.getyx()
        more = False
        while not self.do_exit:
            self.f_string = ''
            self.prompt(more)
            try:
                inp = self.get_line()
            except KeyboardInterrupt:
                self.statusbar.message('KeyboardInterrupt')
                self.scr.addstr('\n')
                self.scr.touchwin()
                self.scr.refresh()
                continue

            self.scr.redrawwin()
            if self.do_exit:
                return

            self.h_i = 0
            self.history.append(inp)
            self.s_hist[-1] += self.f_string
            self.stdout_hist += inp.encode(getpreferredencoding()) + '\n'
# Keep two copies so you can go up and down in the hist:
            if inp:
                self.rl_hist.append(inp + '\n')
            more = self.push(inp) or self.paste_mode

    def size(self):
        """Set instance attributes for x and y top left corner coordinates
        and width and heigth for the window."""
        h, w = stdscr.getmaxyx()
        self.y = 0
        self.w = w
        self.h = h-1
        self.x = 0

    def resize(self):
        """This method exists simply to keep it straight forward when
        initialising a window and resizing it."""
        self.size()
        self.scr.erase()
        self.scr.resize(self.h, self.w)
        self.scr.mvwin(self.y, self.x)
        self.statusbar.resize(refresh=False)
        self.redraw()

    def write(self, s):
        """For overriding stdout defaults"""
        if '\x04' in s:
            for block in s.split('\x04'):
                self.write(block)
            return
        if s.rstrip() and '\x03' in s:
            t = s.split('\x03')[1]
        else:
            t = s

        if isinstance(t, unicode):
            t = t.encode(getpreferredencoding())

        if not self.stdout_hist:
            self.stdout_hist = t
        else:
            self.stdout_hist += t

        self.echo(s)
        self.s_hist.append(s.rstrip())

    def flush(self):
        """Olivier Grisel brought it to my attention that the logging
        module tries to call this method, since it makes assumptions
        about stdout that may not necessarily be true. The docs for
        sys.stdout say:

        "stdout and stderr needn't be built-in file objects: any
         object is acceptable as long as it has a write() method
         that takes a string argument."

        So I consider this to be a bug in logging, and this is a hack
        to fix it, unfortunately. I'm sure it's not the only module
        to do it."""
        pass

    def close(self):
        """See the flush() method docstring."""
        pass

    def echo(self, s, redraw=True):
        """Parse and echo a formatted string with appropriate attributes. It
        uses the formatting method as defined in formatter.py to parse the
        srings. It won't update the screen if it's reevaluating the code (as it
        does with undo)."""
        if isinstance(s, unicode):
            s = s.encode(getpreferredencoding())

        a = get_colpair('output')
        if '\x01' in s:
            rx = re.search('\x01([A-Za-z])([A-Za-z]?)', s)
            if rx:
                fg = rx.groups()[0]
                bg = rx.groups()[1]
                col_num = self._C[fg.lower()]
                if bg and bg != 'I':
                    col_num *= self._C[bg.lower()]

                a = curses.color_pair(int(col_num) + 1)
                if bg == 'I':
                    a = a | curses.A_REVERSE
                s = re.sub('\x01[A-Za-z][A-Za-z]?', '', s)
                if fg.isupper():
                    a = a | curses.A_BOLD
        s = s.replace('\x03', '')
        s = s.replace('\x01', '')


        self.scr.addstr(s, a)

        if redraw and not self.evaluating:
            self.scr.refresh()

    def mvc(self, i, refresh=True):
        """This method moves the cursor relatively from the current
        position, where:
            0 == (right) end of current line
            length of current line len(self.s) == beginning of current line
        and:
            current cursor position + i
            for positive values of i the cursor will move towards the beginning
            of the line, negative values the opposite."""
        y, x = self.scr.getyx()

        if self.cpos == 0 and i < 0:
            return False

        if x == self.ix and y == self.iy and i >= 1:
            return False

        h, w = gethw()
        if x - i < 0:
            y -= 1
            x = w

        if x - i >= w:
            y += 1
            x = 0 + i

        self.cpos += i
        self.scr.move(y, x - i)
        if refresh:
            self.scr.refresh()

        return True

    def bs(self, delete_tabs=True):
        """Process a backspace"""

        y, x = self.scr.getyx()

        if not self.s:
            return

        if x == self.ix and y == self.iy:
            return

        n = 1

        if x == 0:
            y -= 1
            x = gethw()[1]

        if not self.cpos:
# I know the nested if blocks look nasty. :(
            if self.atbol() and delete_tabs:
                n = len(self.s) % OPTS.tab_length
                if not n:
                    n = OPTS.tab_length

            self.s = self.s[:-n]
        else:
            self.s = self.s[:-self.cpos-1] + self.s[-self.cpos:]

        for _ in range(n):
            self.scr.delch(y, x - n)

        return n

    def bs_word(self):
        pos = len(self.s) - self.cpos - 1
# First we delete any space to the left of the cursor.
        while pos >= 0 and self.s[pos] == ' ':
            pos -= self.bs()
# Then we delete a full word.
        while pos >= 0 and self.s[pos] != ' ':
            pos -= self.bs()

    def delete(self):
        """Process a del"""
        if not self.s:
            return

        if self.mvc(-1):
            self.bs(False)

    def cut_to_buffer(self):
        """Clear from cursor to end of line, placing into cut buffer"""
        self.cut_buffer = self.s[-self.cpos:]
        self.s = self.s[:-self.cpos]
        self.cpos = 0
        self.print_line(self.s, clr=True)
        self.scr.redrawwin()
        self.scr.refresh()

    def yank_from_buffer(self):
        """Paste the text from the cut buffer at the current cursor location"""
        self.addstr(self.cut_buffer)
        self.print_line(self.s, clr=True)

    def clrtobol(self):
        """Clear from cursor to beginning of line; usual C-u behaviour"""
        if not self.cpos:
            self.s = ''
        else:
            self.s = self.s[-self.cpos:]

        self.print_line(self.s, clr=True)
        self.scr.redrawwin()
        self.scr.refresh()

    def p_key(self):
        """Process a keypress"""

        if self.c is None:
            return ''

        if self.c == chr(8): # C-Backspace (on my computer anyway!)
            self.clrtobol()
            self.c = '\n'
            # Don't return; let it get handled
        if self.c == chr(27):
            return ''

        if self.c in (chr(127), 'KEY_BACKSPACE'):
            self.bs()
            # Redraw (as there might have been highlighted parens)
            self.print_line('')
            self.print_line(self.s)
            self.complete()
            return ''

        elif self.c == 'KEY_DC': # Del
            self.delete()
            self.complete()
            return ''

        elif self.c == chr(18): # C-r
            self.undo()
            return ''

        elif self.c == 'KEY_UP': # Cursor Up
            self.back()
            return ''

        elif self.c == 'KEY_DOWN': # Cursor Down
            self.fwd()
            return ''

        elif self.c == 'KEY_LEFT': # Cursor Left
            self.mvc(1)
            # Redraw (as there might have been highlighted parens)
            self.print_line(self.s)

        elif self.c == 'KEY_RIGHT': # Cursor Right
            self.mvc(-1)
            # Redraw (as there might have been highlighted parens)
            self.print_line(self.s)

        elif self.c in ("KEY_HOME", '^A', chr(1)): # home or ^A
            self.mvc(len(self.s) - self.cpos)

        elif self.c in ("KEY_END", '^E', chr(5)): # end or ^E
            self.mvc(-self.cpos)

        elif self.c in ('^K', chr(11)): # cut to buffer
            self.cut_to_buffer()
            return ''

        elif self.c in ('^Y', chr(25)): # yank from buffer
            self.yank_from_buffer()
            return ''

        elif self.c in ('^W', chr(23)): # C-w
            self.bs_word()
            self.complete()
            return ''

        elif self.c in ('^U', chr(21)): # C-u
            self.clrtobol()
            return ''

        elif self.c in ('^L', chr(12)): # C-l
            self.s_hist = [self.s_hist[-1]]
            self.highlighted_paren = None
            self.redraw()
            return ''

        elif self.c in (chr(4), '^D'): # C-d
            if not self.s:
                self.do_exit = True
                return None
            else:
                return ''

        elif self.c in key_dispatch[OPTS.save_key]:
            self.write2file()
            return ''

        elif self.c in key_dispatch[OPTS.pastebin_key]:
            self.pastebin()
            return ''

        elif self.c == '\n':
            self.lf()
            return None

        elif self.c == '\t':
            return self.tab()

        elif (not self.c.startswith('KEY_')
              and not unicodedata.category(self.c) == 'Cc'):
            self.addstr(self.c)
            self.print_line(self.s)

        else:
            return ''


        return True

    def tab(self):
        """Process the tab key being hit. If there's only whitespace
        in the line or the line is blank then process a normal tab,
        otherwise attempt to autocomplete to the best match of possible
        choices in the match list."""

        if self.atbol():
            x_pos = len(self.s) - self.cpos
            num_spaces = x_pos % OPTS.tab_length
            if not num_spaces:
                num_spaces = OPTS.tab_length

            self.addstr(' ' * num_spaces)
            self.print_line(self.s)
            return True

        if not OPTS.auto_display_list and not self.list_win_visible:
            self.complete(tab=True)
            return True

        cw = self.cw()
        if not cw:
            return True

        b = self.strbase(self.matches)
        if b:
            self.s += b[len(cw):]
            self.print_line(self.s)
            if len(self.matches) == 1 and OPTS.auto_display_list:
                self.scr.touchwin()
        return True

    def strbase(self, l):
        """Probably not the best way of doing it but this function returns
        a common base string in a list of strings (for tab completion)."""

        if not l:
            return ''
        elif len(l) == 1:
            return l[0]

        sl = sorted(l, key=len)
        for i, c in enumerate(l[-1]):
# I hate myself. Please email seamusmb@gmail.com to call him a dickhead for
# insisting that I make bpython 2.4-compatible. I couldn't be bothered
# refactoring, so ghetto all() it is:
            if not reduce(lambda x, y: (x and y) or False,
                            (k.startswith(l[-1][:i]) for k in sl),
                            True):
                break

        return l[-1][:i-1]

    def atbol(self):
        """Return True or False accordingly if the cursor is at the beginning
        of the line (whitespace is ignored). This exists so that p_key() knows
        how to handle the tab key being pressed - if there is nothing but white
        space before the cursor then process it as a normal tab otherwise
        attempt tab completion."""

        if not self.s.lstrip():
            return True

    def lf(self):
        """Process a linefeed character; it only needs to check the
        cursor position and move appropriately so it doesn't clear
        the current line after the cursor."""
        if self.cpos:
            for _ in range(self.cpos):
                self.mvc(-1)

        # Reprint the line (as there was maybe a highlighted paren in it)
        self.print_line(self.s, newline=True)
        self.echo("\n")

        self.inside_string = next_token_inside_string(self.s,
                                                      self.inside_string)

    def addstr(self, s):
        """Add a string to the current input line and figure out
        where it should go, depending on the cursor position."""
        if not self.cpos:
            self.s += s
        else:
            l = len(self.s)
            self.s = self.s[:l - self.cpos] + s + self.s[l - self.cpos:]

        self.complete()

    def print_line(self, s, clr=False, newline=False):
        """Chuck a line of text through the highlighter, move the cursor
        to the beginning of the line and output it to the screen."""

        if not s:
            clr = True

        if OPTS.syntax and (not self.paste_mode or newline):
            if self.inside_string:
                # A string started in another line is continued in this
                # line
                tokens = PythonLexer().get_tokens(self.inside_string + s)
                token, value = tokens.next()
                if token is Token.String.Doc:
                    tokens = [(Token.String, value[3:])] + list(tokens)
            else:
                tokens = list(PythonLexer().get_tokens(s))
            # Highlight matching parentheses
            def reprint_line(lineno, s, to_replace=[]):
                if lineno < 0:
                    return
                t = list(PythonLexer().get_tokens(s))
                for (i, token) in to_replace:
                    t[i] = token
                o = format(t, BPythonFormatter(OPTS.color_scheme))
                self.scr.move(lineno, 4)
                map(self.echo, o.split('\x04'))

            y, x = self.scr.getyx()
            if self.highlighted_paren:
                # Clear previous highlighted paren
                reprint_line(*self.highlighted_paren)
                self.highlighted_paren = None
            stack = list()
            source = '\n'.join(self.buffer) + '\n%s' % (s, )
            i = line = 0
            pos = 3
            for (token, value) in PythonLexer().get_tokens(source):
                pos += len(value)
                under_cursor = (line == len(self.buffer) and pos == x)
                if token is Token.Punctuation:
                    if value in '({[':
                        if under_cursor:
                            tokens[i] = (Parenthesis, value)
                            # Push marker on the stack
                            stack.append(Parenthesis)
                        else:
                            stack.append((line, i, value))
                    elif value in ')}]':
                        try:
                            opening = stack.pop()
                        except IndexError:
                            # SyntaxError.. more closed parentheses than
                            # opened
                            break
                        if opening is Parenthesis:
                            # Marker found
                            tokens[i] = (Parenthesis, value)
                            break
                        elif under_cursor:
                            tokens[i] = (Parenthesis, value)
                            (line, i, opening) = opening
                            screen_line = y - len(self.buffer) + line
                            if line == len(self.buffer):
                                self.highlighted_paren = (screen_line, s)
                                tokens[i] = (Parenthesis, opening)
                            else:
                                self.highlighted_paren = (screen_line,
                                                          self.buffer[line])
                                # We need to redraw a line
                                reprint_line(
                                    screen_line,
                                    self.buffer[line],
                                    [(i, (Parenthesis, opening))]
                                )
                    elif under_cursor:
                        break
                elif under_cursor:
                    break
                elif token is Token.Text and value == '\n':
                    line += 1
                    i = -1
                    pos = 3
                i += 1
            o = format(tokens, BPythonFormatter(OPTS.color_scheme))
        else:
            o = s

        self.f_string = o
        self.scr.move(self.iy, self.ix)

        if clr:
            self.scr.clrtoeol()

        if clr and not s:
            self.scr.refresh()

        if o:
            for t in o.split('\x04'):
                self.echo(t.rstrip('\n'))

        if self.cpos:
            t = self.cpos
            for _ in range(self.cpos):
                self.mvc(1)
            self.cpos = t

    def get_line(self):
        """Get a line of text and return it
        This function initialises an empty string and gets the
        curses cursor position on the screen and stores it
        for the echo() function to use later (I think).
        Then it waits for key presses and passes them to p_key(),
        which returns None if Enter is pressed (that means "Return",
        idiot)."""

        self.ts = ''

        indent_spaces = len(self.s) - len(self.s.lstrip(' '))

        indent = self.s.rstrip().endswith(':')

        self.s = ''
        self.iy, self.ix = self.scr.getyx()

        if not self.paste_mode:
            for _ in range(indent_spaces // OPTS.tab_length):
                self.c = '\t'
                self.p_key()

        if indent and not self.paste_mode:
            self.c = '\t'
            self.p_key()

        self.c = None
        self.cpos = 0

        while True:
            self.c = self.get_key()
            if self.p_key() is None:
                return self.s

    def clear_current_line(self):
        """This is used as the exception callback for the Interpreter instance.
        It prevents autoindentation from occuring after a traceback."""

        self.inside_string = False
        self.s = ''

    def get_key(self):
        key = ''
        while True:
            try:
                key += self.scr.getkey()
                key = key.decode(getpreferredencoding())
                self.scr.nodelay(False)
            except UnicodeDecodeError:
# Yes, that actually kind of sucks, but I don't see another way to get
# input right
                self.scr.nodelay(True)
            except curses.error:
# I'm quite annoyed with the ambiguity of this exception handler. I previously
# caught "curses.error, x" and accessed x.message and checked that it was "no
# input", which seemed a crappy way of doing it. But then I ran it on a
# different computer and the exception seems to have entirely different
# attributes. So let's hope getkey() doesn't raise any other crazy curses
# exceptions. :)
                self.scr.nodelay(False)
                # XXX What to do here? Raise an exception?
                if key:
                    return key
            else:
                t = time.time()
                self.paste_mode = (t - self.last_key_press <= self.paste_time)
                self.last_key_press = t
                return key
            finally:
                if self.idle:
                    self.idle(self)

class Statusbar(object):
    """This class provides the status bar at the bottom of the screen.
    It has message() and prompt() methods for user interactivity, as
    well as settext() and clear() methods for changing its appearance.

    The check() method needs to be called repeatedly if the statusbar is
    going to be aware of when it should update its display after a message()
    has been called (it'll display for a couple of seconds and then disappear).

    It should be called as:
        foo = Statusbar(stdscr, scr, 'Initial text to display')
    or, for a blank statusbar:
        foo = Statusbar(stdscr, scr)

    It can also receive the argument 'c' which will be an integer referring
    to a curses colour pair, e.g.:
        foo = Statusbar(stdscr, 'Hello', c=4)

    stdscr should be a curses window object in which to put the status bar.
    pwin should be the parent window. To be honest, this is only really here
    so the cursor can be returned to the window properly.

    """

    def __init__(self, scr, pwin, s=None, c=None):
        """Initialise the statusbar and display the initial text (if any)"""
        self.size()
        self.win = newwin(self.h, self.w, self.y, self.x)

        self.s = s or ''
        self._s = self.s
        self.c = c
        self.timer = 0
        self.pwin = pwin
        self.settext(s, c)

    def size(self):
        """Set instance attributes for x and y top left corner coordinates
        and width and heigth for the window."""
        h, w = gethw()
        self.y = h-1
        self.w = w
        self.h = 1
        self.x = 0

    def resize(self, refresh=True):
        """This method exists simply to keep it straight forward when
        initialising a window and resizing it."""
        self.size()
        self.win.mvwin(self.y, self.x)
        self.win.resize(self.h, self.w)
        if refresh:
            self.refresh()

    def refresh(self):
        """This is here to make sure the status bar text is redraw properly
        after a resize."""
        self.settext(self._s)

    def check(self):
        """This is the method that should be called every half second or so
        to see if the status bar needs updating."""
        if not self.timer:
            return

        if time.time() < self.timer:
            return

        self.settext(self._s)

    def message(self, s, n=3):
        """Display a message for a short n seconds on the statusbar and return
        it to its original state."""
        self.timer = time.time() + n
        self.settext(s)

    def prompt(self, s=''):
        """Prompt the user for some input (with the optional prompt 's') and
        return the input text, then restore the statusbar to its original
        value."""

        self.settext(s or '? ', p=True)
        iy, ix = self.win.getyx()

        def bs(s):
            y, x = self.win.getyx()
            if x == ix:
                return s
            s = s[:-1]
            self.win.delch(y, x-1)
            self.win.move(y, x-1)
            return s

        o = ''
        while True:
            c = self.win.getch()

            if c == 127:
                o = bs(o)
                continue

            if c == 27:
                raise ValueError

            if not c or c < 0 or c > 127:
                continue
            c = chr(c)

            if c == '\n':
                break

            self.win.addstr(c, get_colpair('prompt'))
            o += c

        self.settext(self._s)
        return o

    def settext(self, s, c=None, p=False):
        """Set the text on the status bar to a new permanent value; this is the
        value that will be set after a prompt or message. c is the optional
        curses colour pair to use (if not specified the last specified colour
        pair will be used).  p is True if the cursor is expected to stay in the
        status window (e.g. when prompting)."""

        self.win.erase()
        if len(s) >= self.w:
            s = s[:self.w-1]

        self.s = s
        if c:
            self.c = c

        if s:
            if self.c:
                self.win.addstr(s, self.c)
            else:
                self.win.addstr(s)

        if not p:
            self.win.noutrefresh()
            self.pwin.refresh()
        else:
            self.win.refresh()

    def clear(self):
        """Clear the status bar."""
        self.win.clear()


def init_wins(scr, cols):
    """Initialise the two windows (the main repl interface and the little
    status bar at the bottom with some stuff in it)"""
#TODO: Document better what stuff is on the status bar.

    h, w = gethw()

    main_win = newwin(h-1, w, 0, 0)
    main_win.scrollok(True)
    main_win.keypad(1)
# Thanks to Angus Gibson for pointing out this missing line which was causing
# problems that needed dirty hackery to fix. :)

# TODO:
# 
# This should show to be configured keys from bpython.ini
# 
    statusbar = Statusbar(scr, main_win,
        ".:: <C-d> Exit  <C-r> Rewind  <%s> Save  <%s> Pastebin ::." % (OPTS.save_key, OPTS.pastebin_key),
            get_colpair('main'))

    return main_win, statusbar


def sigwinch(unused_scr):
    global DO_RESIZE
    DO_RESIZE = True


def gethw():
    """I found this code on a usenet post, and snipped out the bit I needed,
    so thanks to whoever wrote that, sorry I forgot your name, I'm sure you're
    a great guy.

    It's unfortunately necessary (unless someone has any better ideas) in order
    to allow curses and readline to work together. I looked at the code for
    libreadline and noticed this comment:

        /* This is the stuff that is hard for me.  I never seem to write good
           display routines in C.  Let's see how I do this time. */

    So I'm not going to ask any questions.

    """
    h, w = struct.unpack(
        "hhhh", fcntl.ioctl(orig_stdout, termios.TIOCGWINSZ, "\000"*8))[0:2]
    return h, w


def idle(caller):
    """This is called once every iteration through the getkey()
    loop (currently in the Repl class, see the get_line() method).
    The statusbar check needs to go here to take care of timed
    messages and the resize handlers need to be here to make
    sure it happens conveniently."""

    global stdscr

    if importcompletion.find_coroutine() or caller.paste_mode:
        stdscr.nodelay(True)
        key = stdscr.getch()
        stdscr.nodelay(False)
        curses.ungetch(key)
    caller.statusbar.check()
    caller.check()

    if DO_RESIZE:
        do_resize(caller)


def do_resize(caller):
    """This needs to hack around readline and curses not playing
    nicely together. See also gethw() above."""
    global DO_RESIZE
    h, w = gethw()
    if not h:
# Hopefully this shouldn't happen. :)
        return

    curses.endwin()
    os.environ["LINES"] = str(h)
    os.environ["COLUMNS"] = str(w)
    curses.doupdate()
    DO_RESIZE = False

    caller.resize()
# The list win resizes itself every time it appears so no need to do it here.

class FakeDict(object):
    """Very simple dict-alike that returns a constant value for any key -
    used as a hacky solution to using a colours dict containing colour codes if
    colour initialisation fails."""
    def __init__(self, val):
        self._val = val
    def __getitem__(self, k):
        return self._val

def newwin(*args):
    """Wrapper for curses.newwin to automatically set background colour on any
    newly created window."""
    win = curses.newwin(*args)
    colpair = get_colpair('background')
    win.bkgd(' ', colpair)
    return win


def main_curses(scr):
    """main function for the curses convenience wrapper

    Initialise the two main objects: the interpreter
    and the repl. The repl does what a repl does and lots
    of other cool stuff like syntax highlighting and stuff.
    I've tried to keep it well factored but it needs some
    tidying up, especially in separating the curses stuff
    from the rest of the repl.
    """
    global stdscr
    global DO_RESIZE
    global colors
    DO_RESIZE = False

    signal.signal(signal.SIGWINCH, lambda *_: sigwinch(scr))

    stdscr = scr
    try:
        curses.start_color()
        curses.use_default_colors()
        cols = make_colors()
    except curses.error:
        cols = FakeDict(-1)

    # FIXME: Gargh, bad design results in using globals without a refactor :(
    colors = cols

    scr.timeout(300)

    curses.raw(True)
    main_win, statusbar = init_wins(scr, cols)

    curses.raw(True)

    interpreter = Interpreter()

    repl = Repl(main_win, interpreter, statusbar, idle)
    interpreter.syntaxerror_callback = repl.clear_current_line

    repl._C = cols

    sys.stdout = repl
    sys.stderr = repl


    repl.repl()
    if OPTS.hist_length:
        histfilename = os.path.expanduser('~/.pythonhist')
        with codecs.open(histfilename, 'w', getpreferredencoding(),
                         'ignore') as hfile:
            hfile.writelines(repl.rl_hist[-OPTS.hist_length:])

    return repl.getstdout()


def main(args=None):
    if args is None:
        args = sys.argv

    parser = OptionParser()
    parser.add_option('--config', '-c', default='~/.bpython.ini',
                      help='use CONFIG instead of default config file')
    parser.add_option('--version', '-V', action='store_true',
                      help='print version and exit')

    options, args = parser.parse_args(args)

    if options.version:
        print 'bpython version', __version__,
        print 'on top of Python', sys.version.split()[0]
        print '(C) 2008-2009 Bob Farrell et al. See AUTHORS for detail.'
        return

    if not os.isatty(sys.stdin.fileno()):
        interpreter = code.InteractiveInterpreter()
        interpreter.runsource(sys.stdin.read())
        return

    setlocale(LC_ALL, '')

    tb = None

    path = os.path.expanduser('~/.bpythonrc')   # migrating old configuration file
    if os.path.isfile(path):
        migrate_rc(path)
    loadini(OPTS, options.config)

    try:
        o = curses.wrapper(main_curses)
    except:
        tb = traceback.format_exc()
# I don't know why this is necessary; without it the wrapper doesn't always do
# its job.
        if stdscr is not None:
            stdscr.keypad(0)
            curses.echo()
            curses.nocbreak()
            curses.endwin()

    sys.stdout = orig_stdout
    if tb:
        print tb
        sys.exit(1)

# Fake stdout data so everything's still visible after exiting
    if OPTS.flush_output:
        sys.stdout.write(o)
    sys.stdout.flush()


if __name__ == '__main__':
    main()

# vim: sw=4 ts=4 sts=4 ai et
