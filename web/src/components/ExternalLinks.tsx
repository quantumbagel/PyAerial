import type { FlightDetail } from '../api/types';
import { cleanField } from '../utils/format';
import { Button } from './ui';

interface ExternalLinksProps {
  flightDetail: FlightDetail | null;
  icao: string | null;
}

const path = (value: string) => encodeURIComponent(value);

/** Links to public trackers; every user-derived value is URL-encoded. */
export function externalTrackerLinks(
  flightDetail: Pick<FlightDetail, 'callsign' | 'registration'> | null,
  icao: string | null,
): { label: string; href: string }[] {
  const callsign = cleanField(flightDetail?.callsign);
  const registration = cleanField(flightDetail?.registration);
  const hex = cleanField(icao)?.toLowerCase() ?? null;
  const links: { label: string; href: string }[] = [];
  if (callsign) {
    links.push({ label: 'FlightAware', href: `https://flightaware.com/live/flight/${path(callsign)}` });
  } else if (hex) {
    links.push({ label: 'FlightAware', href: `https://flightaware.com/live/modes/${path(hex)}` });
  }
  if (hex) {
    links.push({
      label: 'ADS-B Exchange',
      href: `https://globe.adsbexchange.com/?icao=${path(hex)}`,
    });
  }
  if (registration) {
    links.push({ label: 'RadarBox', href: `https://www.radarbox.com/data/registration/${path(registration)}` });
  } else if (hex) {
    links.push({ label: 'RadarBox', href: `https://www.radarbox.com/data/mode-s/${path(hex)}` });
  }
  return links;
}

export function ExternalLinks({ flightDetail, icao }: ExternalLinksProps) {
  const links = externalTrackerLinks(flightDetail, icao);
  if (!links.length) return null;
  return (
    <section className="info-section" aria-labelledby="external-trackers-heading">
      <h3 id="external-trackers-heading">External trackers</h3>
      <div className="ui-link-grid">
        {links.map((link) => (
          <Button key={link.label} as="a" variant="link" href={link.href} target="_blank" rel="noreferrer">
            <span>{link.label}</span>
          </Button>
        ))}
      </div>
    </section>
  );
}
