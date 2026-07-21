export function DisconnectedBanner({ visible }: { visible: boolean }) {
  return (
    <div className={`disconnected-banner${visible ? ' disconnected-banner--visible' : ''}`} role="alert" aria-live="assertive">
      <span className="flight-list-spinner disconnected-banner__spinner" aria-hidden="true" />
      <span className="disconnected-banner__text">Reconnecting…</span>
    </div>
  );
}

