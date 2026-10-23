
import { BrowserRouter, Routes, Route, Navigate } from "react-router-dom";
import Login from "./pages/Login";
import Dashboard from "./pages/Dashboard";
import LeadDetails from "./pages/LeadDetails";
import CallTracking from "./pages/CallTracking";
import ProductsCampaigns from "./pages/ProductsCampaigns";
import ProtectedRoute from "./components/ProtectedRoute";

function App() {
  return (
    <BrowserRouter>
      <Routes>
        <Route path="/login" element={<Login />} />

        <Route
          path="/"
          element={
            <ProtectedRoute>
              <Dashboard />
            </ProtectedRoute>
          }
        />

        <Route
          path="/leads/:leadId"
          element={
            <ProtectedRoute>
              <LeadDetails />
            </ProtectedRoute>
          }
        />

        <Route
          path="/call-tracking"
          element={
            <ProtectedRoute>
              <CallTracking />
            </ProtectedRoute>
          }
        />

        <Route
          path="/products-campaigns"
          element={
            <ProtectedRoute>
              <ProductsCampaigns />
            </ProtectedRoute>
          }
        />

        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </BrowserRouter>
  );
}

export default App;
