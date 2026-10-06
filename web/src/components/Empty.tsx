export function Empty({ title, text }: { title: string; text: string }) {
  return (
    <div className="empty">
      <div className="glass"><h2>{title}</h2><div>{text}</div></div>
    </div>
  );
}
