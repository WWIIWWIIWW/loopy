from __future__ import division, absolute_import

__copyright__ = "Copyright (C) 2012-15 Andreas Kloeckner"

__license__ = """
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
"""


import numpy as np

from pymbolic.mapper import CombineMapper

from loopy.tools import is_integer
from loopy.diagnostic import TypeInferenceFailure, DependencyTypeInferenceFailure


# type_context may be:
# - 'i' for integer -
# - 'f' for single-precision floating point
# - 'd' for double-precision floating point
# or None for 'no known context'.

def dtype_to_type_context(target, dtype):
    dtype = np.dtype(dtype)

    if dtype.kind == 'i':
        return 'i'
    if dtype in [np.float64, np.complex128]:
        return 'd'
    if dtype in [np.float32, np.complex64]:
        return 'f'
    if target.is_vector_dtype(dtype):
        return dtype_to_type_context(target, dtype.fields["x"][0])

    return None


# {{{ type inference

class TypeInferenceMapper(CombineMapper):
    def __init__(self, kernel, new_assignments=None):
        """
        :arg new_assignments: mapping from names to either
            :class:`loopy.kernel.data.TemporaryVariable`
            or
            :class:`loopy.kernel.data.KernelArgument`
            instances
        """
        self.kernel = kernel
        if new_assignments is None:
            new_assignments = {}
        self.new_assignments = new_assignments

    # /!\ Introduce caches with care--numpy.float32(x) and numpy.float64(x)
    # are Python-equal (for many common constants such as integers).

    @staticmethod
    def combine(dtypes):
        dtypes = list(dtypes)

        result = dtypes.pop()
        while dtypes:
            other = dtypes.pop()

            if result.isbuiltin and other.isbuiltin:
                if (result, other) in [
                        (np.int32, np.float32), (np.float32, np.int32)]:
                    # numpy makes this a double. I disagree.
                    result = np.dtype(np.float32)
                else:
                    result = (
                            np.empty(0, dtype=result)
                            + np.empty(0, dtype=other)
                            ).dtype
            elif result.isbuiltin and not other.isbuiltin:
                # assume the non-native type takes over
                result = other
            elif not result.isbuiltin and other.isbuiltin:
                # assume the non-native type takes over
                pass
            else:
                if result is not other:
                    raise TypeInferenceFailure(
                            "nothing known about result of operation on "
                            "'%s' and '%s'" % (result, other))

        return result

    def map_sum(self, expr):
        dtypes = []
        small_integer_dtypes = []
        for child in expr.children:
            dtype = self.rec(child)
            if is_integer(child) and abs(child) < 1024:
                small_integer_dtypes.append(dtype)
            else:
                dtypes.append(dtype)

        from pytools import all
        if all(dtype.kind == "i" for dtype in dtypes):
            dtypes.extend(small_integer_dtypes)

        return self.combine(dtypes)

    map_product = map_sum

    def map_quotient(self, expr):
        n_dtype = self.rec(expr.numerator)
        d_dtype = self.rec(expr.denominator)

        if n_dtype.kind in "iu" and d_dtype.kind in "iu":
            # both integers
            return np.dtype(np.float64)

        else:
            return self.combine([n_dtype, d_dtype])

    def map_constant(self, expr):
        if is_integer(expr):
            for tp in [np.int32, np.int64]:
                iinfo = np.iinfo(tp)
                if iinfo.min <= expr <= iinfo.max:
                    return np.dtype(tp)

            else:
                raise TypeInferenceFailure("integer constant '%s' too large" % expr)

        dt = np.asarray(expr).dtype
        if hasattr(expr, "dtype"):
            return expr.dtype
        elif isinstance(expr, np.number):
            # Numpy types are sized
            return np.dtype(type(expr))
        elif dt.kind == "f":
            # deduce the smaller type by default
            return np.dtype(np.float32)
        elif dt.kind == "c":
            if np.complex64(expr) == np.complex128(expr):
                # (COMPLEX_GUESS_LOGIC)
                # No precision is lost by 'guessing' single precision, use that.
                # This at least covers simple cases like '1j'.
                return np.dtype(np.complex64)

            # Codegen for complex types depends on exactly correct types.
            # Refuse temptation to guess.
            raise TypeInferenceFailure("Complex constant '%s' needs to "
                    "be sized for type inference " % expr)
        else:
            raise TypeInferenceFailure("Cannot deduce type of constant '%s'" % expr)

    def map_subscript(self, expr):
        return self.rec(expr.aggregate)

    def map_linear_subscript(self, expr):
        return self.rec(expr.aggregate)

    def map_call(self, expr):
        from pymbolic.primitives import Variable

        identifier = expr.function
        if isinstance(identifier, Variable):
            identifier = identifier.name

        arg_dtypes = tuple(self.rec(par) for par in expr.parameters)

        mangle_result = self.kernel.mangle_function(identifier, arg_dtypes)
        if mangle_result is not None:
            return mangle_result[0]

        raise RuntimeError("no type inference information on "
                "function '%s'" % identifier)

    def map_variable(self, expr):
        if expr.name in self.kernel.all_inames():
            return self.kernel.index_dtype

        result = self.kernel.mangle_symbol(expr.name)
        if result is not None:
            result_dtype, _ = result
            return result_dtype

        obj = self.new_assignments.get(expr.name)

        if obj is None:
            obj = self.kernel.arg_dict.get(expr.name)

        if obj is None:
            obj = self.kernel.temporary_variables.get(expr.name)

        if obj is None:
            raise TypeInferenceFailure("name not known in type inference: %s"
                    % expr.name)

        from loopy.kernel.data import TemporaryVariable, KernelArgument
        import loopy as lp
        if isinstance(obj, TemporaryVariable):
            result = obj.dtype
            if result is lp.auto:
                raise DependencyTypeInferenceFailure(
                        "temporary variable '%s'" % expr.name,
                        expr.name)
            else:
                return result

        elif isinstance(obj, KernelArgument):
            result = obj.dtype
            if result is None:
                raise DependencyTypeInferenceFailure(
                        "argument '%s'" % expr.name,
                        expr.name)
            else:
                return result

        else:
            raise RuntimeError("unexpected type inference "
                    "object type for '%s'" % expr.name)

    map_tagged_variable = map_variable

    def map_lookup(self, expr):
        agg_result = self.rec(expr.aggregate)
        dtype, offset = agg_result.fields[expr.name]
        return dtype

    def map_comparison(self, expr):
        # "bool" is unusable because OpenCL's bool has indeterminate memory
        # format.
        return np.dtype(np.int32)

    map_logical_not = map_comparison
    map_logical_and = map_comparison
    map_logical_or = map_comparison

    def map_reduction(self, expr):
        return expr.operation.result_dtype(
                self.kernel.target, self.rec(expr.expr), expr.inames)

# }}}

# vim: fdm=marker
