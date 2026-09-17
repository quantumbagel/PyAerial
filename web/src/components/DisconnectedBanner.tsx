import type { WsStatus } from '../api/types';
import { Spinner } from './ui';

export function DisconnectedBanner({
  status,
  message,
}: {
  status: WsStatus;
  message?: string | null;
}) {
  const visible = status !== 'connected' || Boolean(message);
  const text =
    status !== 'connected'
      ? status === 'reconnecting'
        ? 'Reconnecting…'
        : 'Connecting…'
      : message || '';
  return (
    <div
      className={`disconnected-banner${visible ? ' disconnected-banner--visible' : ''}`}
      role="alert"
      aria-live="assertive"
      aria-hidden={!visible}
    >
      {status !== 'connected' ? (
        <Spinner className="disconnected-banner__spinner" />
      ) : null}
      <span className="disconnected-banner__text">{text}</span>
    </div>
  );
}
