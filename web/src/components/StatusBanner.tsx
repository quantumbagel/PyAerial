import type { StatusBanner as StatusBannerData } from '../utils/emptyStates';
import { Spinner } from './ui';

/** Connection / pipeline status shown over the top of the map, below its toolbar. */
export function StatusBanner({ banner }: { banner: StatusBannerData | null }) {
  return (
    <div
      className={`status-banner${banner ? ' status-banner--visible' : ''}`}
      role="status"
      aria-live="polite"
      aria-hidden={!banner}
    >
      {banner?.pending ? <Spinner className="status-banner__spinner" /> : null}
      <span className="status-banner__text">{banner?.text ?? ''}</span>
    </div>
  );
}
