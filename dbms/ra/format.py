def format_table(schema, rows, limit=None):
    names = schema.qnames()
    rows = list(rows)
    shown = rows if limit is None else rows[:limit]
    cells = [[str(v) if not isinstance(v, float) else f"{v:.4f}" for v in r] for r in shown]
    w = [max([len(n)] + [len(c[i]) for c in cells]) for i, n in enumerate(names)]
    lines = [" | ".join(n.ljust(x) for n, x in zip(names, w)).rstrip(), "-+-".join("-" * x for x in w)]
    lines += [" | ".join(v.ljust(x) for v, x in zip(c, w)).rstrip() for c in cells]
    lines.append(f"({len(rows)} tuple{'s' if len(rows) != 1 else ''})")
    if limit is not None and len(rows) > limit:
        lines.append(f"... {len(rows) - limit} more tuples not shown (use --limit)")
    return "\n".join(lines)
