import type { WsStatus } from '../api/types';
import { Spinner } from './ui';

export function DisconnectedBanner({ status }: { status: WsStatus }) {
  const visible = status !== 'connected';
  const text = status === 'reconnecting' ? 'Reconnecting…' : 'Connecting…';
  return (
    <div
      className={`disconnected-banner${visible ? ' disconnected-banner--visible' : ''}`}
      role="alert"
      aria-live="assertive"
      aria-hidden={!visible}
    >
      <Spinner className="disconnected-banner__spinner" />
      <span className="disconnected-banner__text">{text}</span>
    </div>
  );
}
