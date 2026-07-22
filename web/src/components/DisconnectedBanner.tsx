import { Spinner } from './ui';

export function DisconnectedBanner({ visible }: { visible: boolean }) {
  return (
    <div className={`disconnected-banner${visible ? ' disconnected-banner--visible' : ''}`} role="alert" aria-live="assertive">
      <Spinner className="disconnected-banner__spinner" />
      <span className="disconnected-banner__text">Reconnecting…</span>
    </div>
  );
}
