import type { FlightDetail } from '../api/types';
import { Button } from './ui';

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
      <div className="ui-link-grid">
        <Button
          as="a"
          variant="link"
          href={
            callsign
              ? `https://flightaware.com/live/flight/${callsign}`
              : `https://flightaware.com/live/modes/${icao.toLowerCase()}`
          }
          target="_blank"
          rel="noreferrer"
        >
          <span>FlightAware</span>
        </Button>
        <Button
          as="a"
          variant="link"
          href={`https://globe.adsbexchange.com/?icao=${icao.toLowerCase()}`}
          target="_blank"
          rel="noreferrer"
        >
          <span>ADS-B Exchange</span>
        </Button>
        <Button
          as="a"
          variant="link"
          href={
            registration
              ? `https://www.radarbox.com/data/registration/${registration}`
              : `https://www.radarbox.com/data/mode-s/${icao.toLowerCase()}`
          }
          target="_blank"
          rel="noreferrer"
        >
          <span>RadarBox</span>
        </Button>
      </div>
    </div>
  );
}
