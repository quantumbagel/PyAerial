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
    <div id="map-controls" role="toolbar" aria-label="Map controls">
      <div className="map-toolbar-group">
        {followVisible && (
          <button
            id="follow-btn"
            className={`toolbar-btn${followActive ? ' active' : ''}`}
            type="button"
            title="Follow selected aircraft"
            aria-pressed={followActive}
            onClick={onToggleFollow}
          >
            {followLabel}
          </button>
        )}
        <button
          id="zones-btn"
          className={`toolbar-btn${zonesActive ? ' active' : ''}`}
          type="button"
          title="Show configured geofence zones"
          aria-pressed={zonesActive}
          onClick={onToggleZones}
        >
          {zonesLabel}
        </button>
        <button
          id="paths-btn"
          className={`toolbar-btn${pathsActive ? ' active' : ''}`}
          type="button"
          title="Show flight paths for all visible aircraft"
          aria-pressed={pathsActive}
          onClick={onTogglePaths}
        >
          {pathsLabel}
        </button>
      </div>
      <div className="map-toolbar-divider" aria-hidden="true" />
      <div className="map-toolbar-group">
        <button
          id="zoom-in-btn"
          className="toolbar-btn map-zoom-btn"
          type="button"
          title="Zoom in"
          aria-label="Zoom in"
          onClick={onZoomIn}
        >
          +
        </button>
        <button
          id="zoom-out-btn"
          className="toolbar-btn map-zoom-btn"
          type="button"
          title="Zoom out"
          aria-label="Zoom out"
          onClick={onZoomOut}
        >
          −
        </button>
      </div>
    </div>
  );
}
