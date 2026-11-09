import { Fragment } from 'react';
import type { Integrity } from '../api/types';
import { LIST_SEP, formatNumber, formatPosition } from '../utils/format';
import { checkLabel, integrityLabel, integrityScore, sortedChecks } from '../utils/integrity';

const STATUS_TONE: Record<Integrity['status'], string> = {
  spoofed: 'alert',
  suspect: 'warn',
  ok: 'emphasis',
  unknown: 'muted',
};

/** Drawer section listing the spoofing checks behind an aircraft's score. */
export function IntegritySection({ integrity }: { integrity?: Integrity | null }) {
  if (!integrity) return null;
  const checks = sortedChecks(integrity);
  const flagged = checks.filter(([, check]) => check.status !== 'pass');
  const passed = checks.filter(([, check]) => check.status === 'pass');
  const emitter = integrity.emitter;
  const score = integrityScore(integrity);
  return (
    <section className="info-section">
      <h3>Integrity</h3>
      <dl className="ui-fields">
        <dt className="ui-field-label">Status</dt>
        <dd className={`ui-field-value ui-field-value--${STATUS_TONE[integrity.status] ?? 'muted'}`}>
          {integrityLabel(integrity)}
          {score != null && <span className="ui-field-sub">Score {formatNumber(score, 2)}</span>}
        </dd>
        {integrity.rebroadcast && (
          <>
            <dt className="ui-field-label">Heard via</dt>
            <dd className="ui-field-value ui-field-value--muted">
              Ground rebroadcast only (TIS-B/ADS-R), so receiver signal checks don't apply
            </dd>
          </>
        )}
        {flagged.map(([name, check]) => (
          <Fragment key={name}>
            <dt className="ui-field-label">{checkLabel(name)}</dt>
            <dd className={`ui-field-value ui-field-value--${check.status === 'fail' ? 'alert' : 'warn'}`}>
              {check.detail}
            </dd>
          </Fragment>
        ))}
        {emitter && (
          <>
            <dt className="ui-field-label">Transmitter</dt>
            <dd className="ui-field-value ui-field-value--alert">
              {formatPosition(emitter.latitude, emitter.longitude)}
              {emitter.offset_km != null && Number.isFinite(emitter.offset_km) && (
                <span className="ui-field-sub">
                  {formatNumber(emitter.offset_km, 1)} km from the claimed position
                </span>
              )}
            </dd>
          </>
        )}
        {passed.length > 0 && (
          <>
            <dt className="ui-field-label">Consistent</dt>
            <dd className="ui-field-value ui-field-value--muted">
              {passed.map(([name]) => checkLabel(name)).join(LIST_SEP)}
            </dd>
          </>
        )}
      </dl>
    </section>
  );
}
