import { useEffect, useState } from 'react';
import * as api from '../api/client';
import { ORIGIN_REJECTED_MESSAGE, describeError } from '../api/errors';
import { connectLiveSocket } from '../api/liveSocket';
import { POLICY_VIOLATION, TRY_AGAIN_LATER } from '../api/reconnectingSocket';
import type { AppConfig, WsStatus, ZonesData } from '../api/types';

/** Socket status plus the station config and zones, (re)loaded on every connect. */
export function useBootstrap() {
  const [zonesData, setZonesData] = useState<ZonesData | null>(null);
  const [appConfig, setAppConfig] = useState<AppConfig | null>(null);
  const [zonesError, setZonesError] = useState<string | null>(null);
  const [configError, setConfigError] = useState<string | null>(null);
  const [wsStatus, setWsStatus] = useState<WsStatus>('connecting');
  const [wsRejectReason, setWsRejectReason] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const load = () => {
      api
        .fetchZones()
        .then((data) => {
          if (cancelled) return;
          setZonesData(data);
          setZonesError(null);
        })
        .catch((err) => {
          if (cancelled) return;
          console.error('Failed to fetch zones', err);
          setZonesError(describeError(err, 'Could not load zones.'));
        });
      api
        .fetchConfig()
        .then((data) => {
          if (cancelled) return;
          setAppConfig(data);
          setConfigError(null);
        })
        .catch((err) => {
          if (cancelled) return;
          console.error('Failed to fetch config', err);
          setConfigError(describeError(err, 'Could not load the station config.'));
        });
    };
    const unsubscribe = connectLiveSocket({
      onOpen: () => {
        setWsStatus('connected');
        setWsRejectReason(null);
        load();
      },
      onClose: (code) => {
        if (code === POLICY_VIOLATION) {
          setWsStatus('rejected');
          setWsRejectReason(ORIGIN_REJECTED_MESSAGE);
          return;
        }
        if (code === TRY_AGAIN_LATER) {
          setWsStatus('busy');
          return;
        }
        setWsStatus((prev) => (prev === 'connecting' ? 'connecting' : 'reconnecting'));
      },
    });
    return () => {
      cancelled = true;
      unsubscribe();
    };
  }, []);

  return {
    zonesData,
    appConfig,
    wsStatus,
    /** Set when the server refused this origin (1008); nothing will load. */
    rejectedMessage: wsRejectReason,
    bootstrapError: zonesError || configError,
  };
}
