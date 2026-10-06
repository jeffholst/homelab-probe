import { Route, Routes } from "react-router-dom";

import { RequireAuth } from "../auth/RequireAuth";
import { Shell } from "../components/Shell";
import { HomePage } from "../pages/HomePage";
import { LoginPage } from "../pages/LoginPage";
import { NotFoundPage } from "../pages/NotFoundPage";
import { ProfilePage } from "../pages/ProfilePage";
import { SetupPage } from "../pages/SetupPage";
import { SetupGate } from "./SetupGate";

/** The routes. The router itself is chosen by the caller (the browser's in main.tsx, a memory router in tests). */
export function App() {
  return (
    <Routes>
      <Route element={<SetupGate />}>
        <Route path="/setup/*" element={<SetupPage />} />
        <Route path="/login" element={<LoginPage />} />
        <Route element={<RequireAuth />}>
          <Route element={<Shell />}>
            <Route index element={<HomePage />} />
            <Route path="profile" element={<ProfilePage />} />
            <Route path="*" element={<NotFoundPage />} />
          </Route>
        </Route>
      </Route>
    </Routes>
  );
}
