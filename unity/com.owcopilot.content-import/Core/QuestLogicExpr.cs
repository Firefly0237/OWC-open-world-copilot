// 任务逻辑布尔表达式的词法 + 递归下降解析（owcopilot.logic.expr 的逐规则 C# 移植，UnityEngine-free）。
// A rule-by-rule C# transliteration of the FROZEN Python parser src/owcopilot/logic/expr.py — the
// canonical recursive-descent form (Nystrom, Crafting Interpreters "Parsing Expressions"; Norvell's
// classic-solution one-method-per-precedence-level shape). Each grammar rule maps to one method,
// lowest precedence calling higher, exactly as _Parser does: ParseOr -> ParseAnd -> ParseNot ->
// ParseCompare -> ParseAtom. Keeping it line-for-line comparable is the anti-divergence property the
// cross-language differential test depends on — DO NOT re-derive the grammar or "optimize" into a
// Pratt/precedence-climbing parser (we have only 4 fixed levels; the extra generality would break
// the transliteration invariant). Any intentional deviation from expr.py must carry a ⚠ comment.
//
// Grammar (lowest to highest precedence), identical to expr.py:
//     or   := and ('or' and)*
//     and  := not ('and' not)*
//     not  := 'not' not | compare
//     cmp  := atom (('=='|'!='|'<'|'>'|'<='|'>=') atom)?
//     atom := '(' or ')' | 'true' | 'false' | INT | STRING | REF
// Expressions are DATA: this only builds an AST; QuestLogicEval walks it. There is no eval/exec, so
// a malformed or adversarial expression can only raise LogicSyntaxException, never execute.

using System.Collections.Generic;
using System.Globalization;
using System.Text.RegularExpressions;

namespace OWCopilot.ContentImport.Core
{
    // --- AST (mirrors the frozen dataclasses in expr.py) -----------------------------------------

    public abstract class Expr
    {
    }

    public sealed class BoolLit : Expr
    {
        public bool Value;
    }

    public sealed class IntLit : Expr
    {
        public long Value; // long (not int): Newtonsoft deserializes JSON integers to Int64.
    }

    public sealed class StrLit : Expr
    {
        public string Value;
    }

    public sealed class Ref : Expr
    {
        public string Name;
    }

    public sealed class Not : Expr
    {
        public Expr Operand;
    }

    public sealed class BoolOp : Expr
    {
        public string Op; // "and" | "or"
        public Expr Left;
        public Expr Right;
    }

    public sealed class Compare : Expr
    {
        public string Op; // "==" "!=" "<" ">" "<=" ">="
        public Expr Left;
        public Expr Right;
    }

    /// <summary>Raised when an expression cannot be parsed (mirrors expr.py LogicSyntaxError, a
    /// ValueError subclass). Never raised from evaluation of an already-parsed tree.</summary>
    public sealed class LogicSyntaxException : System.Exception
    {
        public LogicSyntaxException(string message) : base(message)
        {
        }
    }

    public static class QuestLogicExpr
    {
        private const int MaxLen = 2000; // no legitimate logic expression is longer
        private const int MaxDepth = 64; // cap parenthesis nesting so deep input cannot overflow

        private static readonly HashSet<string> Keywords =
            new HashSet<string> { "and", "or", "not", "true", "false" };

        private static readonly HashSet<string> CompareOps =
            new HashSet<string> { "==", "!=", "<", ">", "<=", ">=" };

        // The exact token alternation from expr.py's _TOKEN. Matched anchored at the scan position
        // (verified by the m.Index == pos check below), mirroring Python's re.match(src, pos).
        private static readonly Regex TokenRe = new Regex(
            "==|!=|<=|>=|[<>()]|\\d+|'[^']*'|\"[^\"]*\"|[A-Za-z_][A-Za-z0-9_:.]*",
            RegexOptions.Compiled);

        private static readonly Regex RefRe =
            new Regex("\\A[A-Za-z_][A-Za-z0-9_:.]*\\z", RegexOptions.Compiled);

        /// <summary>Parse an expression source string into an AST, or raise LogicSyntaxException.
        /// Callers treat an empty string as 'no expression' (always true) and must not call this on
        /// it — mirrors expr.py parse_expr.</summary>
        public static Expr Parse(string source)
        {
            if (source.Length > MaxLen)
            {
                throw new LogicSyntaxException("expression too long");
            }
            return new Parser(Tokenize(source)).Parse();
        }

        /// <summary>Every variable/state reference an expression reads (for symbol/seed building) —
        /// mirrors expr.py refs_in.</summary>
        public static HashSet<string> RefsIn(Expr expr)
        {
            var refs = new HashSet<string>();
            Collect(expr, refs);
            return refs;
        }

        private static void Collect(Expr expr, HashSet<string> refs)
        {
            switch (expr)
            {
                case Ref r:
                    refs.Add(r.Name);
                    break;
                case Not n:
                    Collect(n.Operand, refs);
                    break;
                case BoolOp op:
                    Collect(op.Left, refs);
                    Collect(op.Right, refs);
                    break;
                case Compare c:
                    Collect(c.Left, refs);
                    Collect(c.Right, refs);
                    break;
            }
        }

        private static List<string> Tokenize(string src)
        {
            var tokens = new List<string>();
            int pos = 0;
            int n = src.Length;
            while (pos < n)
            {
                if (char.IsWhiteSpace(src[pos]))
                {
                    pos++;
                    continue;
                }
                Match match = TokenRe.Match(src, pos);
                if (!match.Success || match.Index != pos)
                {
                    throw new LogicSyntaxException(
                        "unexpected character '" + src[pos] + "' at position " + pos);
                }
                tokens.Add(match.Value);
                pos = match.Index + match.Length;
            }
            return tokens;
        }

        private sealed class Parser
        {
            private readonly List<string> _toks;
            private int _i;
            private int _depth;

            public Parser(List<string> tokens)
            {
                _toks = tokens;
            }

            private string Peek() => _i < _toks.Count ? _toks[_i] : null;

            private string Next()
            {
                string token = Peek();
                if (token == null)
                {
                    throw new LogicSyntaxException("unexpected end of expression");
                }
                _i++;
                return token;
            }

            public Expr Parse()
            {
                Expr expr = ParseOr();
                if (Peek() != null)
                {
                    throw new LogicSyntaxException("unexpected trailing token '" + Peek() + "'");
                }
                return expr;
            }

            private Expr ParseOr()
            {
                Expr left = ParseAnd();
                while (Peek() == "or")
                {
                    Next();
                    left = new BoolOp { Op = "or", Left = left, Right = ParseAnd() };
                }
                return left;
            }

            private Expr ParseAnd()
            {
                Expr left = ParseNot();
                while (Peek() == "and")
                {
                    Next();
                    left = new BoolOp { Op = "and", Left = left, Right = ParseNot() };
                }
                return left;
            }

            private Expr ParseNot()
            {
                if (Peek() == "not")
                {
                    Next();
                    return new Not { Operand = ParseNot() };
                }
                return ParseCompare();
            }

            private Expr ParseCompare()
            {
                Expr left = ParseAtom();
                string op = Peek();
                if (op != null && CompareOps.Contains(op))
                {
                    Next();
                    return new Compare { Op = op, Left = left, Right = ParseAtom() };
                }
                return left;
            }

            private Expr ParseAtom()
            {
                string token = Next();
                if (token == "(")
                {
                    _depth++;
                    if (_depth > MaxDepth)
                    {
                        throw new LogicSyntaxException("expression nesting too deep");
                    }
                    Expr inner = ParseOr();
                    if (Peek() != ")")
                    {
                        throw new LogicSyntaxException("missing closing ')'");
                    }
                    Next();
                    _depth--;
                    return inner;
                }
                if (token == "true")
                {
                    return new BoolLit { Value = true };
                }
                if (token == "false")
                {
                    return new BoolLit { Value = false };
                }
                if (token[0] == '\'' || token[0] == '"')
                {
                    return new StrLit { Value = token.Substring(1, token.Length - 2) };
                }
                if (IsAllDigits(token))
                {
                    // ⚠ Deliberate, documented divergence from the Python oracle: Python's int is
                    // arbitrary-precision, so a literal beyond Int64 evaluates fine there; here it
                    // becomes a syntax error, which Truthy() degrades to condition=false (safe,
                    // never a crash). The audit rejects such literals long before runtime, and the
                    // cross-language trace corpus deliberately contains none.
                    if (!long.TryParse(token, NumberStyles.None, CultureInfo.InvariantCulture, out long intValue))
                    {
                        throw new LogicSyntaxException("integer literal out of range: " + token);
                    }
                    return new IntLit { Value = intValue };
                }
                if (Keywords.Contains(token))
                {
                    throw new LogicSyntaxException("unexpected keyword '" + token + "'");
                }
                if (RefRe.IsMatch(token))
                {
                    return new Ref { Name = token };
                }
                throw new LogicSyntaxException("unexpected token '" + token + "'");
            }

            private static bool IsAllDigits(string token)
            {
                if (token.Length == 0)
                {
                    return false;
                }
                foreach (char c in token)
                {
                    if (c < '0' || c > '9')
                    {
                        return false;
                    }
                }
                return true;
            }
        }
    }
}
