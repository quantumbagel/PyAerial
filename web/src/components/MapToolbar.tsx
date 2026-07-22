import { Button } from './ui';

interface MapToolbarProps {
  followVisible: boolean;
  followActive: boolean;
  zonesActive: boolean;
  pathsActive: boolean;
  followLabel: string;
  zonesLabel: string;
  pathsLabel: string;
  onToggleFollow: () => void;
  onToggleZones: () => void;
  onTogglePaths: () => void;
  onZoomIn: () => void;
  onZoomOut: () => void;
}

export function MapToolbar({
  followVisible,
  followActive,
  zonesActive,
  pathsActive,
  followLabel,
  zonesLabel,
  pathsLabel,
  onToggleFollow,
  onToggleZones,
  onTogglePaths,
  onZoomIn,
  onZoomOut,
}: MapToolbarProps) {
  return (
    <div id="map-controls" className="ui-toolbar" role="toolbar" aria-label="Map controls">
      <div className="ui-btn-group">
        {followVisible && (
          <Button
            id="follow-btn"
            variant="toggle"
            active={followActive}
            title="Follow selected aircraft"
            aria-pressed={followActive}
            onClick={onToggleFollow}
          >
            {followLabel}
          </Button>
        )}
        <Button
          id="zones-btn"
          variant="toggle"
          active={zonesActive}
          title="Show configured geofence zones"
          aria-pressed={zonesActive}
          onClick={onToggleZones}
        >
          {zonesLabel}
        </Button>
        <Button
          id="paths-btn"
          variant="toggle"
          active={pathsActive}
          title="Show flight paths for all visible aircraft"
          aria-pressed={pathsActive}
          onClick={onTogglePaths}
        >
          {pathsLabel}
        </Button>
      </div>
      <div className="ui-toolbar__divider" aria-hidden="true" />
      <div className="ui-btn-group">
        <Button
          id="zoom-in-btn"
          variant="toggle"
          zoom
          title="Zoom in"
          aria-label="Zoom in"
          onClick={onZoomIn}
        >
          +
        </Button>
        <Button
          id="zoom-out-btn"
          variant="toggle"
          zoom
          title="Zoom out"
          aria-label="Zoom out"
          onClick={onZoomOut}
        >
          −
        </Button>
      </div>
    </div>
  );
}
