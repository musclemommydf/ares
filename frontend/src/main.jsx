import React from 'react'
import ReactDOM from 'react-dom/client'
import App from './App'
import ConnectGate from './components/Auth/ConnectGate'
import './App.css'
import 'leaflet/dist/leaflet.css'

// Fix Leaflet default marker icon paths
import L from 'leaflet'
import markerIcon from 'leaflet/dist/images/marker-icon.png'
import markerIcon2x from 'leaflet/dist/images/marker-icon-2x.png'
import markerShadow from 'leaflet/dist/images/marker-shadow.png'

delete L.Icon.Default.prototype._getIconUrl
L.Icon.Default.mergeOptions({
  iconUrl: markerIcon,
  iconRetinaUrl: markerIcon2x,
  shadowUrl: markerShadow,
})

ReactDOM.createRoot(document.getElementById('root')).render(
  <React.StrictMode>
    <ConnectGate>
      <App />
    </ConnectGate>
  </React.StrictMode>
)
