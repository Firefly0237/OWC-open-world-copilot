// 表达式的显式树遍历求值 + 严格类型规则 + 条件真值判定（semantics.py evaluate/_compare 与
// simulate._truthy 的逐行移植，UnityEngine-free）。求值是对已解析 AST 的显式遍历，绝无 eval。
// A line-for-line port of the FROZEN strict evaluator semantics.py::evaluate/_compare plus
// simulate._truthy. Strict rules, transliterated exactly (any deliberate divergence gets a ⚠):
//   * and/or/not require a Bool operand — an int is NOT a bool (AsBool throws otherwise);
//   * order comparisons (< > <= >=) require BOTH operands be Int (a bool is NOT an int) — else throw;
//   * ==/!= use Python generic equality (RunnerValue.PyEquals): strings only equal strings, bool and
//     int compare numerically;
//   * a broken/unknown-ref expression used as a CONDITION is unsatisfiable (false) via the try/catch
//     in Truthy — never an exception at the state-machine level.

using System.Collections.Generic;

namespace OWCopilot.ContentImport.Core
{
    /// <summary>Raised when a parsed expression cannot be evaluated against a state (e.g. unknown
    /// ref, type error). Mirrors semantics.py LogicEvalError (a ValueError subclass).</summary>
    public sealed class LogicEvalException : System.Exception
    {
        public LogicEvalException(string message) : base(message)
        {
        }
    }

    public static class QuestLogicEval
    {
        /// <summary>Evaluate a parsed expression against a concrete state. Assumes it type-checked;
        /// an unknown reference throws rather than guessing — mirrors semantics.evaluate.</summary>
        public static RunnerValue Evaluate(Expr expr, IReadOnlyDictionary<string, RunnerValue> state)
        {
            switch (expr)
            {
                case BoolLit b:
                    return RunnerValue.OfBool(b.Value);
                case IntLit i:
                    return RunnerValue.OfInt(i.Value);
                case StrLit s:
                    return RunnerValue.OfString(s.Value);
                case Ref r:
                    if (!state.TryGetValue(r.Name, out RunnerValue value))
                    {
                        throw new LogicEvalException("reference not in state: " + r.Name);
                    }
                    return value;
                case Not n:
                    return RunnerValue.OfBool(!AsBool(Evaluate(n.Operand, state)));
                case BoolOp op:
                {
                    bool left = AsBool(Evaluate(op.Left, state));
                    if (op.Op == "and")
                    {
                        return RunnerValue.OfBool(left && AsBool(Evaluate(op.Right, state)));
                    }
                    return RunnerValue.OfBool(left || AsBool(Evaluate(op.Right, state)));
                }
                case Compare c:
                    return CompareValues(c.Op, Evaluate(c.Left, state), Evaluate(c.Right, state));
            }
            throw new LogicEvalException("unknown expression node");
        }

        /// <summary>Condition truth for the state machine — mirrors simulate._truthy: an empty/blank
        /// source is vacuously true; otherwise parse + evaluate + Python-truthiness, and ANY parse or
        /// eval failure (broken expr / unknown ref / type error) means unsatisfiable (false).</summary>
        public static bool Truthy(IReadOnlyDictionary<string, RunnerValue> state, string source)
        {
            if (string.IsNullOrWhiteSpace(source))
            {
                return true;
            }
            try
            {
                return Evaluate(QuestLogicExpr.Parse(source), state).IsTruthy();
            }
            catch (LogicSyntaxException)
            {
                return false; // a broken expression is not satisfiable (audit already flags it)
            }
            catch (LogicEvalException)
            {
                return false;
            }
        }

        private static bool AsBool(RunnerValue value)
        {
            if (value.Kind != RunnerValueKind.Bool)
            {
                throw new LogicEvalException("expected a boolean, got " + value.ToWireString());
            }
            return value.Bool;
        }

        private static RunnerValue CompareValues(string op, RunnerValue left, RunnerValue right)
        {
            if (op == "==")
            {
                return RunnerValue.OfBool(left.PyEquals(right));
            }
            if (op == "!=")
            {
                return RunnerValue.OfBool(!left.PyEquals(right));
            }
            // Order ops: both operands must be Int, and a bool is explicitly NOT an int.
            if (left.Kind != RunnerValueKind.Int)
            {
                throw new LogicEvalException("'" + op + "' needs integers, got " + left.ToWireString());
            }
            if (right.Kind != RunnerValueKind.Int)
            {
                throw new LogicEvalException("'" + op + "' needs integers, got " + right.ToWireString());
            }
            long a = left.Int;
            long b = right.Int;
            switch (op)
            {
                case "<":
                    return RunnerValue.OfBool(a < b);
                case ">":
                    return RunnerValue.OfBool(a > b);
                case "<=":
                    return RunnerValue.OfBool(a <= b);
                default:
                    return RunnerValue.OfBool(a >= b); // ">="
            }
        }
    }
}
