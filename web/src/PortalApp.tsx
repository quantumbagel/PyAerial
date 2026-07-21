import { AlertToasts } from './components/AlertToasts';
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
    notificationsEnabled,
    enableNotifications,
    mapRef,
    alertNotifications,
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
    disableFollow,
  } = usePortalApp();

  return (
    <>
      <Sidebar
        portalView={portalView}
        sidebarTab={portal.sidebarTab}
        searchQuery={searchQuery}
        flights={filteredFlights}
        alerts={filteredAlerts}
        allAlerts={portal.alertsData}
        activeFlightId={selection.activeFlightId}
        activeAlertId={selection.activeAlertId}
        flightCount={flightCount}
        flightSortField={flightSortField}
        flightSortDirection={flightSortDirection}
        unreadAlertsCount={portal.unreadAlertsCount}
        isLoadingFlights={portal.isLoadingFlights}
        isLoadingAlerts={portal.isLoadingAlerts}
        flightsError={portal.flightsError}
        alertsError={portal.alertsError}
        notificationsEnabled={notificationsEnabled}
        onEnableNotifications={enableNotifications}
        onSwitchPortalView={portal.switchPortalView}
        onFlightSortChange={setFlightSort}
        onFlightSortDirectionToggle={toggleFlightSortDirection}
        onSwitchSidebarTab={portal.handleSwitchSidebarTab}
        onSearchChange={setSearchQuery}
        onSelectFlight={selection.selectFlight}
        onSelectAlert={selection.selectAlert}
        onAlertsScroll={portal.handleAlertsScroll}
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
            activeAlertId={selection.activeAlertId}
            flightAlerts={selection.flightAlerts}
            flightTelemetry={selection.flightTelemetry}
            drawerTab={selection.drawerTab}
            selectedTelemetryPoint={selection.selectedTelemetryPoint}
            appConfig={portal.appConfig}
            selectionError={selection.selectionError}
            onSelectTelemetryPoint={selection.setSelectedTelemetryPoint}
            onClose={selection.closeDrawer}
            onSwitchTab={selection.setDrawerTab}
            onSelectAlert={selection.selectAlert}
          />
        }
      />
      <AlertToasts
        toasts={alertNotifications.toasts}
        onSelectAlert={selection.selectAlert}
        onDismiss={alertNotifications.dismissToast}
      />
      <DisconnectedBanner visible={!portal.wsConnected} />
    </>
  );
}
