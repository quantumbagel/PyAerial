import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { PortalApp } from './PortalApp';
import './styles/portal.css';
import { applyPaletteTokens } from './utils/colors';

applyPaletteTokens();

createRoot(document.getElementById('root')!).render(
  <StrictMode>
    <PortalApp />
  </StrictMode>,
);
