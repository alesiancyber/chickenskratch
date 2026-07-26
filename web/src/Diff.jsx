/* Renders diffhtml.diff_rows — the promoted diff computation — under
   the design tokens. Semantic color lives only here. */

function segs(list, kind) {
  return (list || []).map((s, i) =>
    s.mark ? <mark key={i} className={kind}>{s.text}</mark> : s.text
  );
}

export default function Diff({ rows }) {
  if (!rows || rows.length === 0) return null;
  return (
    <div className="diff-wrap">
      <table className="diff">
        <tbody>
          {rows.map((r, i) => {
            if (r.kind === "skip")
              return (
                <tr key={i} className="skip">
                  <td colSpan={4}>⋯ {r.count} unchanged line(s)</td>
                </tr>
              );
            if (r.kind === "context")
              return (
                <tr key={i}>
                  <td className="n">{r.ln}</td>
                  <td className="cell">{r.text}</td>
                  <td className="n">{r.rn}</td>
                  <td className="cell">{r.text}</td>
                </tr>
              );
            return (
              <tr key={i}>
                <td className="n">{r.ln ?? ""}</td>
                <td className={"cell" + (r.ln ? " del" : "")}>{segs(r.left, "del")}</td>
                <td className="n">{r.rn ?? ""}</td>
                <td className={"cell" + (r.rn ? " add" : "")}>{segs(r.right, "add")}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}
