import { useCallback, useMemo } from 'react';
import { DetailsDrawer } from './components/DetailsDrawer';
import { DisconnectedBanner } from './components/DisconnectedBanner';
import { MapView } from './components/MapView';
import { Sidebar } from './components/Sidebar';
import { usePortalApp } from './hooks/usePortalApp';
import type { TelemetryPoint } from './api/types';

export function PortalApp() {
  const app = usePortalApp();
  const {
    portalView,
    selection,
    bootstrap,
    feed,
    paths,
    mapRef,
    disableFollow,
    setZonesVisible,
  } = app;
  const { zonesData, appConfig } = bootstrap;
  const {
    activeFlightId,
    followSelectedPlane,
    setFollowSelectedPlane,
    selectFlight,
    setSelectedTelemetryPoint,
    drawerTab,
  } = selection;

  const selectedFlightSummary = useMemo(
    () => feed.flights.find((f) => f.flight_id === activeFlightId) ?? null,
    [feed.flights, activeFlightId],
  );

  const rawAntenna = useMemo(
    () => (appConfig ? { home: appConfig.home, receivers: appConfig.receivers ?? [] } : null),
    [appConfig],
  );

  const handleSelectRawIcao = useCallback(
    (icao: string) => {
      const match = feed.flights.find((f) => f.icao.toLowerCase() === icao.toLowerCase());
      if (match) void selectFlight(match.flight_id);
    },
    [feed.flights, selectFlight],
  );

  const handleToggleFollow = useCallback(() => {
    if (!activeFlightId) return;
    if (followSelectedPlane) {
      setFollowSelectedPlane(false);
      return;
    }
    setFollowSelectedPlane(true);
    if (selectedFlightSummary?.latitude != null && selectedFlightSummary.longitude != null) {
      mapRef.current.focusOn(selectedFlightSummary.latitude, selectedFlightSummary.longitude, 11);
    }
  }, [activeFlightId, followSelectedPlane, setFollowSelectedPlane, selectedFlightSummary, mapRef]);

  const handleRetrySelection = useCallback(() => {
    if (activeFlightId) void selectFlight(activeFlightId, drawerTab);
  }, [activeFlightId, drawerTab, selectFlight]);

  const handleSelectTelemetryPoint = useCallback(
    (point: TelemetryPoint) => {
      disableFollow();
      setSelectedTelemetryPoint(point);
    },
    [disableFollow, setSelectedTelemetryPoint],
  );

  return (
    <>
      <Sidebar
        portalView={portalView}
        sidebarTab={app.sidebarTab}
        searchQuery={app.searchQuery}
        flights={app.filteredFlights}
        alerts={app.filteredAlerts}
        allAlerts={feed.alerts}
        unreadAlertsCount={app.unreadAlertsCount}
        serverStats={feed.stats}
        activeFlightId={activeFlightId}
        activeAlertId={selection.activeAlertId}
        flightCount={app.flightCount}
        flightSortField={app.flightSort.field}
        flightSortDirection={app.flightSort.direction}
        alertSortField={app.alertSort.field}
        alertSortDirection={app.alertSort.direction}
        isLoadingFlights={feed.isLoadingFlights}
        isLoadingAlerts={feed.isLoadingAlerts}
        flightsError={feed.flightsError}
        alertsError={feed.alertsError}
        onRetryFlights={app.retryFlights}
        onRetryAlerts={app.retryAlerts}
        onSwitchPortalView={app.switchPortalView}
        onFlightSortChange={app.flightSort.setField}
        onFlightSortDirectionToggle={app.flightSort.toggleDirection}
        onAlertSortChange={app.alertSort.setField}
        onAlertSortDirectionToggle={app.alertSort.toggleDirection}
        onSwitchSidebarTab={app.switchSidebarTab}
        onSearchChange={app.setSearchQuery}
        historySinceDate={app.historySinceDate}
        historyUntilDate={app.historyUntilDate}
        onHistorySinceChange={app.setHistorySinceDate}
        onHistoryUntilChange={app.setHistoryUntilDate}
        onSelectFlight={selectFlight}
        onSelectAlert={selection.selectAlert}
        onLoadMoreAlerts={app.loadMoreAlerts}
        onLoadMoreFlights={app.loadMoreFlights}
        zones={zonesData?.zones}
        alertColors={zonesData?.alert_colors}
        rawAntenna={rawAntenna}
        onSelectRawIcao={handleSelectRawIcao}
      />
      <MapView
        flights={feed.flights}
        filteredFlights={app.filteredFlights}
        activeFlightId={activeFlightId}
        selectedTelemetryPoint={selection.selectedTelemetryPoint}
        followSelectedPlane={followSelectedPlane}
        zonesVisible={app.zonesVisible}
        showAllPaths={paths.showAllPaths}
        zonesData={zonesData}
        appConfig={appConfig}
        pathCoords={paths.pathCoords}
        pathTelemetry={paths.pathTelemetry}
        pathAlerts={paths.pathAlerts}
        onSelectFlight={selectFlight}
        onFollowDisabled={disableFollow}
        onToggleFollow={handleToggleFollow}
        onToggleZones={() => setZonesVisible((v) => !v)}
        onTogglePaths={() => paths.setShowAllPaths((v) => !v)}
        followLabel={followSelectedPlane ? 'Following' : 'Follow'}
        zonesLabel={app.zonesVisible ? 'Zones On' : 'Zones Off'}
        pathsLabel={paths.showAllPaths ? 'Paths On' : 'Paths Off'}
        followVisible={!!activeFlightId}
        followActive={followSelectedPlane}
        zonesActive={app.zonesVisible}
        pathsActive={paths.showAllPaths}
        mapRef={mapRef}
        drawer={
          <DetailsDrawer
            open={selection.drawerOpen}
            flightDetail={selection.flightDetail}
            flightSummary={selectedFlightSummary}
            isLoading={selection.isLoading}
            activeAlertId={selection.activeAlertId}
            flightAlerts={selection.flightAlerts}
            flightTelemetry={selection.flightTelemetry}
            drawerTab={drawerTab}
            selectedTelemetryPoint={selection.selectedTelemetryPoint}
            appConfig={appConfig}
            zones={zonesData?.zones}
            alertColors={zonesData?.alert_colors}
            selectionError={selection.selectionError}
            onRetry={handleRetrySelection}
            onSelectTelemetryPoint={handleSelectTelemetryPoint}
            onClose={selection.closeDrawer}
            onSwitchTab={selection.setDrawerTab}
            onSelectAlert={selection.selectAlert}
          />
        }
      />
      <DisconnectedBanner
        status={bootstrap.wsStatus}
        message={
          bootstrap.bootstrapError ||
          (app.engineIdle
            ? 'Tracking engine is idle. Start `pyaerial run`; frozen aircraft will expire shortly.'
            : null)
        }
      />
    </>
  );
}
