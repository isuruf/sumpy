from __future__ import print_function, division

__copyright__ = """
Copyright (C) 2017 Matt Wala
Copyright (C) 2006-2016 SymPy Development Team
"""

# {{{ license and original license

__license__ = """
Modifications from original are under the following license:

Copyright (C) 2017 Matt Wala

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in
all copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN
THE SOFTWARE.

===========================================================================

Based on sympy/simplify/cse_main.py from SymPy 1.0, license as follows:

Copyright (c) 2006-2016 SymPy Development Team

All rights reserved.

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

  a. Redistributions of source code must retain the above copyright notice,
     this list of conditions and the following disclaimer.
  b. Redistributions in binary form must reproduce the above copyright
     notice, this list of conditions and the following disclaimer in the
     documentation and/or other materials provided with the distribution.
  c. Neither the name of SymPy nor the names of its contributors
     may be used to endorse or promote products derived from this software
     without specific prior written permission.


THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES, INCLUDING, BUT NOT LIMITED TO, THE
IMPLIED WARRANTIES OF MERCHANTABILITY AND FITNESS FOR A PARTICULAR PURPOSE
ARE DISCLAIMED. IN NO EVENT SHALL THE REGENTS OR CONTRIBUTORS BE LIABLE FOR
ANY DIRECT, INDIRECT, INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL
DAMAGES (INCLUDING, BUT NOT LIMITED TO, PROCUREMENT OF SUBSTITUTE GOODS OR
SERVICES; LOSS OF USE, DATA, OR PROFITS; OR BUSINESS INTERRUPTION) HOWEVER
CAUSED AND ON ANY THEORY OF LIABILITY, WHETHER IN CONTRACT, STRICT
LIABILITY, OR TORT (INCLUDING NEGLIGENCE OR OTHERWISE) ARISING IN ANY WAY
OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH
DAMAGE.
"""

# }}}

from symengine.sympy_compat import Basic, Mul, Add, Pow, Symbol
from sympy.core.function import _coeff_isneg
from sympy.core.compatibility import iterable
from sympy.utilities.iterables import numbered_symbols


__doc__ = """

Common subexpression elimination
--------------------------------

.. autofunction:: cse

"""
args_dict = dict()

def get_args(expr):
    if expr in args_dict:
        return args_dict[expr]
    return expr.args

# {{{ cse pre/postprocessing

def preprocess_for_cse(expr, optimizations):
    """
    Preprocess an expression to optimize for common subexpression elimination.

    :arg expr: A sympy expression, the target expression to optimize.
    :arg optimizations: A list of (callable, callable) pairs,
        the (preprocessor, postprocessor) pairs.

    :return: The transformed expression.
    """
    for pre, post in optimizations:
        if pre is not None:
            expr = pre(expr)
    return expr


def postprocess_for_cse(expr, optimizations):
    """
    Postprocess an expression after common subexpression elimination to
    return the expression to canonical sympy form.

    :arg expr: sympy expression, the target expression to transform.
    :arg optimizations: A list of (callable, callable) pairs (optional),
        the (preprocessor, postprocessor) pairs.  The postprocessors will be
        applied in reversed order to undo the effects of the preprocessors
        correctly.

    :return: The transformed expression.
    """
    for pre, post in reversed(optimizations):
        if post is not None:
            expr = post(expr)
    return expr

# }}}


# {{{ opt cse

class FuncArgTracker(object):
    """
    A class which manages a mapping from functions to arguments and an inverse
    mapping from arguments to functions.
    """

    def __init__(self, funcs):
        # To minimize the number of symbolic comparisons, all function arguments
        # get assigned a value number.
        self.value_numbers = {}
        self.value_number_to_value = []

        # Both of these maps use integer indices for arguments / functions.
        self.arg_to_funcset = []
        self.func_to_argset = []

        for func_i, func in enumerate(funcs):
            func_argset = set()

            for func_arg in get_args(func):
                arg_number = self.get_or_add_value_number(func_arg)
                func_argset.add(arg_number)
                self.arg_to_funcset[arg_number].add(func_i)

            self.func_to_argset.append(func_argset)

    def get_args_in_value_order(self, argset):
        """
        Return the list of arguments in sorted order according to their value
        numbers.
        """
        return [self.value_number_to_value[argn] for argn in sorted(argset)]

    def get_or_add_value_number(self, value):
        """
        Return the value number for the given argument.
        """
        nvalues = len(self.value_numbers)
        value_number = self.value_numbers.setdefault(value, nvalues)
        if value_number == nvalues:
            self.value_number_to_value.append(value)
            self.arg_to_funcset.append(set())
        return value_number

    def stop_arg_tracking(self, func_i):
        """
        Remove the function func_i from the argument to function mapping.
        """
        for arg in self.func_to_argset[func_i]:
            self.arg_to_funcset[arg].remove(func_i)

    def get_common_arg_candidates(self, argset, min_func_i, threshold=2):
        """
        Return a dict whose keys are function numbers. The entries of the dict are
        the number of arguments said function has in common with `argset`. Entries
        have at least `threshold` items in common.
        """
        from collections import defaultdict
        count_map = defaultdict(lambda: 0)

        for arg in argset:
            for func_i in self.arg_to_funcset[arg]:
                if func_i >= min_func_i:
                    count_map[func_i] += 1

        return dict(
            (k, v) for k, v in count_map.items()
            if v >= threshold)

    def get_subset_candidates(self, argset, restrict_to_funcset=None):
        """
        Return a set of functions each of which whose argument list contains
        `argset`, optionally filtered only to contain functions in
        `restrict_to_funcset`.
        """
        iarg = iter(argset)

        indices = set(
            fi for fi in self.arg_to_funcset[next(iarg)])

        if restrict_to_funcset is not None:
            indices &= restrict_to_funcset

        for arg in iarg:
            indices &= self.arg_to_funcset[arg]

        return indices

    def update_func_argset(self, func_i, new_argset):
        """
        Update a function with a new set of arguments.
        """
        new_args = set(new_argset)
        old_args = self.func_to_argset[func_i]

        for deleted_arg in old_args - new_args:
            self.arg_to_funcset[deleted_arg].remove(func_i)
        for added_arg in new_args - old_args:
            self.arg_to_funcset[added_arg].add(func_i)

        self.func_to_argset[func_i].clear()
        self.func_to_argset[func_i].update(new_args)


def match_common_args(func_class, funcs, opt_subs):
    """
    Recognize and extract common subexpressions of function arguments within a
    set of function calls. For instance, for the following function calls::

        x + z + y
        sin(x + y)

    this will extract a common subexpression of `x + y`::

        w = x + y
        w + z
        sin(w)

    The function we work with is assumed to be associative and commutative.

    :arg func_class: The function class (e.g. Add, Mul)
    :arg funcs: A list of function calls
    :arg opt_subs: A dictionary of substitutions which this function may update
    """

    # Sort to ensure that whole-function subexpressions come before the items
    # that use them.
    funcs = sorted(funcs, key=lambda f: len(get_args(f)))
    arg_tracker = FuncArgTracker(funcs)

    changed = set()

    from sumpy.tools import OrderedSet

    for i in range(len(funcs)):
        common_arg_candidates = arg_tracker.get_common_arg_candidates(
                arg_tracker.func_to_argset[i], i + 1, threshold=2)

        # Sort the candidates in order of match size.
        # This makes us try combining smaller matches first.
        common_arg_candidates = OrderedSet(sorted(
                common_arg_candidates.keys(),
                key=lambda k: (common_arg_candidates[k], k)))

        while common_arg_candidates:
            j = common_arg_candidates.pop(last=False)

            com_args = arg_tracker.func_to_argset[i].intersection(
                    arg_tracker.func_to_argset[j])

            if len(com_args) <= 1:
                # This may happen if a set of common arguments was already
                # combined in a previous iteration.
                continue

            # For all sets, replace the common symbols by the function
            # over them, to allow recursive matches.

            diff_i = arg_tracker.func_to_argset[i].difference(com_args)
            if diff_i:
                # com_func needs to be unevaluated to allow for recursive matches.
                args_orig = arg_tracker.get_args_in_value_order(com_args)
                t = func_class(*args_orig)
                args_dict[t] = args_orig
                com_func = t
                com_func_number = arg_tracker.get_or_add_value_number(com_func)
                arg_tracker.update_func_argset(i, diff_i | set([com_func_number]))
                changed.add(i)
            else:
                # Treat the whole expression as a CSE.
                #
                # The reason this needs to be done is somewhat subtle. Within
                # tree_cse(), to_eliminate only contains expressions that are
                # seen more than once. The problem is unevaluated expressions
                # do not compare equal to the evaluated equivalent. So
                # tree_cse() won't mark funcs[i] as a CSE if we use an
                # unevaluated version.
                com_func = funcs[i]
                com_func_number = arg_tracker.get_or_add_value_number(funcs[i])

            diff_j = arg_tracker.func_to_argset[j].difference(com_args)
            arg_tracker.update_func_argset(j, diff_j | set([com_func_number]))
            changed.add(j)

            for k in arg_tracker.get_subset_candidates(
                    com_args, common_arg_candidates):
                diff_k = arg_tracker.func_to_argset[k].difference(com_args)
                arg_tracker.update_func_argset(k, diff_k | set([com_func_number]))
                changed.add(k)

        if i in changed:
            args_orig = arg_tracker.get_args_in_value_order(arg_tracker.func_to_argset[i])
            t = func_class(*args_orig)
            args_dict[t] = args_orig
            opt_subs[funcs[i]] = t

        arg_tracker.stop_arg_tracking(i)


def opt_cse(exprs):
    """
    Find optimization opportunities in Adds, Muls, Pows and negative coefficient
    Muls

    :arg exprs: A list of sympy expressions: the expressions to optimize.
    :return: A dictionary of expression substitutions
    """
    opt_subs = dict()

    from sumpy.tools import OrderedSet
    adds = OrderedSet()
    muls = OrderedSet()

    seen_subexp = set()

    # {{{ look for optimization opportunities, clean up minus signs

    def find_opts(expr):
        if not isinstance(expr, Basic):
            return

        if expr.is_Atom:
            return

        if iterable(expr):
            for item in expr:
                find_opts(item)
            return

        if expr in seen_subexp:
            return expr

        seen_subexp.add(expr)

        for arg in get_args(expr):
            find_opts(arg)

        if _coeff_isneg(expr):
            neg_expr = -expr
            if not neg_expr.is_Atom:
                t = Mul(-1, neg_expr)
                args_dict[t] = (-1, neg_expr)
                opt_subs[expr] = t
                seen_subexp.add(neg_expr)
                expr = neg_expr

        if isinstance(expr, Mul):
            muls.add(expr)

        elif isinstance(expr, Add):
            adds.add(expr)

        elif isinstance(expr, Pow):
            base, exp = get_args(expr)
            if _coeff_isneg(exp):
                t = Pow(Pow(base, -exp), -1)
                args_dict[t] = (Pow(base, -exp), -1)
                opt_subs[expr] = t

    # }}}

    for e in exprs:
        if isinstance(e, Basic):
            find_opts(e)

    match_common_args(Add, list(adds), opt_subs)
    match_common_args(Mul, list(muls), opt_subs)

    return opt_subs

# }}}


# {{{ tree cse

def tree_cse(exprs, symbols, opt_subs=None):
    """
    Perform raw CSE on an expression tree, taking opt_subs into account.

    :arg exprs: A list of sympy expressions to reduce
    :arg symbols: An infinite iterator yielding unique Symbols used to label
        the common subexpressions which are pulled out.
    :arg opt_subs: A dictionary of expression substitutions to be
        substituted before any CSE action is performed.

    :return: A pair (replacements, reduced exprs)
    """
    if opt_subs is None:
        opt_subs = dict()

    # {{{ find repeated sub-expressions and used symbols

    to_eliminate = set()

    seen_subexp = set()
    excluded_symbols = set()

    def find_repeated(expr):
        if not isinstance(expr, Basic):
            return

        if expr.is_Atom:
            if expr.is_Symbol:
                excluded_symbols.add(expr)
            return

        if iterable(expr):
            args = expr

        else:
            if expr in seen_subexp:
                to_eliminate.add(expr)
                return

            seen_subexp.add(expr)

            if expr in opt_subs:
                expr = opt_subs[expr]

            args = get_args(expr)

        for arg in args:
            find_repeated(arg)

    # }}}

    for e in exprs:
        if isinstance(e, Basic):
            find_repeated(e)

    # {{{ rebuild tree

    # Remove symbols from the generator that conflict with names in the expressions.
    symbols = (symbol for symbol in symbols if symbol not in excluded_symbols)

    replacements = []

    subs = dict()

    def rebuild(expr):
        if not isinstance(expr, Basic):
            return expr

        if not get_args(expr):
            return expr

        if iterable(expr):
            new_args = [rebuild(arg) for arg in expr]
            return expr.func(*new_args)

        if expr in subs:
            return subs[expr]

        orig_expr = expr
        if expr in opt_subs:
            expr = opt_subs[expr]

        new_args = [rebuild(arg) for arg in get_args(expr)]
        if new_args != get_args(expr):
            new_expr = expr.func(*new_args)
        else:
            new_expr = expr

        if orig_expr in to_eliminate:
            try:
                sym = next(symbols)
            except StopIteration:
                raise ValueError("Symbols iterator ran out of symbols.")

            subs[orig_expr] = sym
            replacements.append((sym, new_expr))
            return sym

        return new_expr

    # }}}

    reduced_exprs = []
    for e in exprs:
        if isinstance(e, Basic):
            reduced_e = rebuild(e)
        else:
            reduced_e = e
        reduced_exprs.append(reduced_e)

    return replacements, reduced_exprs

# }}}


def cse(exprs, symbols=None, optimizations=None):
    """
    Perform common subexpression elimination on an expression.

    :arg exprs: A list of sympy expressions, or a single sympy expression to reduce
    :arg symbols: An iterator yielding unique Symbols used to label the
        common subexpressions which are pulled out. The ``numbered_symbols``
        generator from sympy is useful. The default is a stream of symbols of the
        form "x0", "x1", etc. This must be an infinite iterator.
    :arg optimizations: A list of (callable, callable) pairs consisting of
        (preprocessor, postprocessor) pairs of external optimization functions.

    :return: This returns a pair ``(replacements, reduced_exprs)``.

        * ``replacements`` is a list of (Symbol, expression) pairs consisting of
          all of the common subexpressions that were replaced. Subexpressions
          earlier in this list might show up in subexpressions later in this list.
        * ``reduced_exprs`` is a list of sympy expressions. This contains the
          reduced expressions with all of the replacements above.
    """
    if isinstance(exprs, Basic):
        exprs = [exprs]

    exprs = list(exprs)

    if optimizations is None:
        optimizations = []

    # Preprocess the expressions to give us better optimization opportunities.
    reduced_exprs = [preprocess_for_cse(e, optimizations) for e in exprs]

    if symbols is None:
        symbols = numbered_symbols(cls=Symbol)
    else:
        # In case we get passed an iterable with an __iter__ method instead of
        # an actual iterator.
        symbols = iter(symbols)

    # Find other optimization opportunities.
    opt_subs = opt_cse(reduced_exprs)

    # Main CSE algorithm.
    replacements, reduced_exprs = tree_cse(reduced_exprs, symbols, opt_subs)

    # Postprocess the expressions to return the expressions to canonical form.
    for i, (sym, subtree) in enumerate(replacements):
        subtree = postprocess_for_cse(subtree, optimizations)
        replacements[i] = (sym, subtree)
    reduced_exprs = [postprocess_for_cse(e, optimizations) for e in reduced_exprs]

    return replacements, reduced_exprs

# vim: fdm=marker
