import { Fragment } from 'react';
import type { Integrity } from '../api/types';
import { LIST_SEP, formatPosition } from '../utils/format';
import { checkLabel, integrityLabel, sortedChecks } from '../utils/integrity';

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
  return (
    <section className="info-section">
      <h3>Integrity</h3>
      <dl className="ui-fields">
        <dt className="ui-field-label">Status</dt>
        <dd className={`ui-field-value ui-field-value--${STATUS_TONE[integrity.status]}`}>
          {integrityLabel(integrity)}
          <span className="ui-field-sub">Score {integrity.score.toFixed(2)} of 1</span>
        </dd>
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
              <span className="ui-field-sub">{emitter.offset_km} km from the claimed position</span>
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
