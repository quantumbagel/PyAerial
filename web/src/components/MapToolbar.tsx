import { Button } from './ui';

interface MapToolbarProps {
  followVisible: boolean;
  followActive: boolean;
  zonesActive: boolean;
  pathsActive: boolean;
  onToggleFollow: () => void;
  onToggleZones: () => void;
  onTogglePaths: () => void;
  onZoomIn: () => void;
  onZoomOut: () => void;
  /** Small screens: show the button that opens the sidebar panel. */
  onToggleSidebar?: () => void;
  sidebarOpen?: boolean;
  sidebarId?: string;
}

/** Map controls. Toggles keep a fixed label; their state is aria-pressed (and styling). */
export function MapToolbar({
  followVisible,
  followActive,
  zonesActive,
  pathsActive,
  onToggleFollow,
  onToggleZones,
  onTogglePaths,
  onZoomIn,
  onZoomOut,
  onToggleSidebar,
  sidebarOpen = false,
  sidebarId,
}: MapToolbarProps) {
  return (
    <div className="map-controls ui-toolbar" role="toolbar" aria-label="Map controls">
      {onToggleSidebar && (
        <>
          <div className="ui-btn-group map-controls__sidebar">
            <Button
              variant="toggle"
              active={sidebarOpen}
              aria-expanded={sidebarOpen}
              aria-controls={sidebarId}
              title="Show flights and alerts"
              onClick={onToggleSidebar}
            >
              <span aria-hidden="true">☰</span> List
            </Button>
          </div>
          <div className="ui-toolbar__divider map-controls__sidebar" aria-hidden="true" />
        </>
      )}
      <div className="ui-btn-group">
        {followVisible && (
          <Button
            variant="toggle"
            active={followActive}
            title="Keep the selected aircraft centred"
            aria-pressed={followActive}
            onClick={onToggleFollow}
          >
            Follow
          </Button>
        )}
        <Button
          variant="toggle"
          active={zonesActive}
          title="Show configured geofence zones"
          aria-pressed={zonesActive}
          onClick={onToggleZones}
        >
          Zones
        </Button>
        <Button
          variant="toggle"
          active={pathsActive}
          title="Show flight paths for all listed aircraft"
          aria-pressed={pathsActive}
          onClick={onTogglePaths}
        >
          Paths
        </Button>
      </div>
      <div className="ui-toolbar__divider" aria-hidden="true" />
      <div className="ui-btn-group">
        <Button variant="toggle" zoom title="Zoom in" aria-label="Zoom in" onClick={onZoomIn}>
          <span aria-hidden="true">+</span>
        </Button>
        <Button variant="toggle" zoom title="Zoom out" aria-label="Zoom out" onClick={onZoomOut}>
          <span aria-hidden="true">−</span>
        </Button>
      </div>
    </div>
  );
}
