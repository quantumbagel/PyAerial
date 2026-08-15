import { DetailsDrawer } from './components/DetailsDrawer';
import { DisconnectedBanner } from './components/DisconnectedBanner';
import { MapView } from './components/MapView';
import { Sidebar } from './components/Sidebar';
import { usePortalApp } from './hooks/usePortalApp';

export function PortalApp() {
  const {
    portalView,
    searchQuery,
    setSearchQuery,
    zonesVisible,
    setZonesVisible,
    mapRef,
    selection,
    portal,
    paths,
    filteredFlights,
    filteredAlerts,
    flightCount,
    flightSortField,
    flightSortDirection,
    setFlightSort,
    toggleFlightSortDirection,
    alertSortField,
    alertSortDirection,
    setAlertSort,
    toggleAlertSortDirection,
    disableFollow,
  } = usePortalApp();

  const selectedFlightSummary =
    portal.flightsData.find((f) => f.flight_id === selection.activeFlightId) || null;

  return (
    <>
      <Sidebar
        portalView={portalView}
        sidebarTab={portal.sidebarTab}
        searchQuery={searchQuery}
        flights={filteredFlights}
        alerts={filteredAlerts}
        allAlerts={portal.alertsData}
        unreadAlertsCount={portal.unreadAlertsCount}
        serverStats={portal.serverStats}
        activeFlightId={selection.activeFlightId}
        activeAlertId={selection.activeAlertId}
        flightCount={flightCount}
        flightSortField={flightSortField}
        flightSortDirection={flightSortDirection}
        alertSortField={alertSortField}
        alertSortDirection={alertSortDirection}
        isLoadingFlights={portal.isLoadingFlights}
        isLoadingAlerts={portal.isLoadingAlerts}
        flightsError={portal.flightsError}
        alertsError={portal.alertsError}
        onRetryFlights={portal.retryFlights}
        onRetryAlerts={portal.retryAlerts}
        onSwitchPortalView={portal.switchPortalView}
        onFlightSortChange={setFlightSort}
        onFlightSortDirectionToggle={toggleFlightSortDirection}
        onAlertSortChange={setAlertSort}
        onAlertSortDirectionToggle={toggleAlertSortDirection}
        onSwitchSidebarTab={portal.handleSwitchSidebarTab}
        onSearchChange={setSearchQuery}
        onSelectFlight={selection.selectFlight}
        onSelectAlert={selection.selectAlert}
        onAlertsScroll={portal.handleAlertsScroll}
        zones={portal.zonesData?.zones}
        alertColors={portal.zonesData?.alert_colors}
      />
      <MapView
        flights={portal.flightsData}
        filteredFlights={filteredFlights}
        activeFlightId={selection.activeFlightId}
        selectedTelemetryPoint={selection.selectedTelemetryPoint}
        followSelectedPlane={selection.followSelectedPlane}
        zonesVisible={zonesVisible}
        showAllPaths={paths.showAllPaths}
        zonesData={portal.zonesData}
        appConfig={portal.appConfig}
        pathCoords={paths.pathCoords}
        pathTelemetry={paths.pathTelemetry}
        pathAlerts={paths.pathAlerts}
        onSelectFlight={selection.selectFlight}
        onFollowDisabled={disableFollow}
        onToggleFollow={() => {
          if (!selection.activeFlightId) return;
          if (selection.followSelectedPlane) {
            selection.setFollowSelectedPlane(false);
          } else {
            selection.setFollowSelectedPlane(true);
            const flight = portal.flightsData.find((f) => f.flight_id === selection.activeFlightId);
            if (flight?.latitude != null && flight?.longitude != null && mapRef.current.map) {
              mapRef.current.map.setView(
                [flight.latitude, flight.longitude],
                Math.max(mapRef.current.map.getZoom(), 11),
              );
            }
          }
        }}
        onToggleZones={() => setZonesVisible((v) => !v)}
        onTogglePaths={() => paths.setShowAllPaths((v) => !v)}
        followLabel={selection.followSelectedPlane ? 'Following' : 'Follow'}
        zonesLabel={zonesVisible ? 'Zones On' : 'Zones Off'}
        pathsLabel={paths.showAllPaths ? 'Paths On' : 'Paths Off'}
        followVisible={!!selection.activeFlightId}
        followActive={selection.followSelectedPlane}
        zonesActive={zonesVisible}
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
            drawerTab={selection.drawerTab}
            selectedTelemetryPoint={selection.selectedTelemetryPoint}
            appConfig={portal.appConfig}
            zones={portal.zonesData?.zones}
            alertColors={portal.zonesData?.alert_colors}
            selectionError={selection.selectionError}
            onRetry={() => {
              if (selection.activeFlightId) {
                selection.selectFlight(selection.activeFlightId, selection.drawerTab);
              }
            }}
            onSelectTelemetryPoint={(point) => {
              disableFollow();
              selection.setSelectedTelemetryPoint(point);
            }}
            onClose={selection.closeDrawer}
            onSwitchTab={selection.setDrawerTab}
            onSelectAlert={selection.selectAlert}
          />
        }
      />
      <DisconnectedBanner visible={!portal.wsConnected} />
    </>
  );
}
