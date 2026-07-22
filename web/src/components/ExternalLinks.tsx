import type { FlightDetail } from '../api/types';

interface ExternalLinksProps {
  flightDetail: FlightDetail | null;
  icao: string;
}

export function ExternalLinks({ flightDetail, icao }: ExternalLinksProps) {
  const callsign = flightDetail?.callsign?.trim();
  const registration = flightDetail?.registration?.trim();
  return (
    <div className="info-section">
      <h3>External Trackers</h3>
      <div className="external-links-grid">
        <a
          href={
            callsign
              ? `https://flightaware.com/live/flight/${callsign}`
              : `https://flightaware.com/live/modes/${icao.toLowerCase()}`
          }
          target="_blank"
          rel="noreferrer"
          className="external-link-btn"
        >
          <span>FlightAware</span>
        </a>
        <a
          href={`https://globe.adsbexchange.com/?icao=${icao.toLowerCase()}`}
          target="_blank"
          rel="noreferrer"
          className="external-link-btn"
        >
          <span>ADS-B Exchange</span>
        </a>
        <a
          href={
            registration
              ? `https://www.radarbox.com/data/registration/${registration}`
              : `https://www.radarbox.com/data/mode-s/${icao.toLowerCase()}`
          }
          target="_blank"
          rel="noreferrer"
          className="external-link-btn"
        >
          <span>RadarBox</span>
        </a>
      </div>
    </div>
  );
}
