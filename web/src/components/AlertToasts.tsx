import type { Alert } from '../api/types';

interface AlertToastsProps {
  toasts: { id: string; alert: Alert }[];
  onSelectAlert: (alert: Alert) => void;
  onDismiss: (id: string) => void;
}

export function AlertToasts({ toasts, onSelectAlert, onDismiss }: AlertToastsProps) {
  return (
    <div className="toast-container">
      {toasts.map((toast) => {
        const levelClass = toast.alert.level?.toLowerCase() === 'alert' ? 'level-alert' : 'level-warn';
        const displayLvl = toast.alert.level?.toUpperCase() || 'WARNING';
        return (
          <div
            key={toast.id}
            className={`toast-alert ${levelClass}`}
            onClick={() => {
              onSelectAlert(toast.alert);
              onDismiss(toast.id);
            }}
          >
            <div className="toast-header">
              <span className="toast-title">
                {toast.alert.level?.toLowerCase() === 'alert' ? '🚨' : '⚠️'} {displayLvl}: {toast.alert.callsign || 'Unknown'}
              </span>
              <button
                type="button"
                className="toast-close"
                onClick={(e) => {
                  e.stopPropagation();
                  onDismiss(toast.id);
                }}
              >
                &times;
              </button>
            </div>
            <div className="toast-body">
              Met conditions in zone <strong>{toast.alert.zone || 'Zone'}</strong>.<br />
              Alt: {toast.alert.altitude ? toast.alert.altitude + ' m' : 'N/A'} | ETA: {toast.alert.eta ? Math.round(toast.alert.eta) + 's' : 'N/A'}
            </div>
          </div>
        );
      })}
    </div>
  );
}
