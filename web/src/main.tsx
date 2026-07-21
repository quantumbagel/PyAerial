import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { PortalApp } from './PortalApp';
import 'leaflet/dist/leaflet.css';
import './styles/portal.css';

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <PortalApp />
  </StrictMode>,
);
