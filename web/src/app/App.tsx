import { Route, Routes } from "react-router-dom";

import { RequireAuth } from "../auth/RequireAuth";
import { Shell } from "../components/Shell";
import { DashboardPage } from "../pages/DashboardPage";
import { LoginPage } from "../pages/LoginPage";
import { NotFoundPage } from "../pages/NotFoundPage";
import { ProfilePage } from "../pages/ProfilePage";
import { SetupPage } from "../setup/SetupPage";

/** The routes. The router itself is chosen by the caller (the browser's in main.tsx, a memory router in tests). */
export function App() {
  return (
    <Routes>
      <Route path="/login" element={<LoginPage />} />
      <Route path="/setup" element={<SetupPage />} />
      <Route element={<RequireAuth />}>
        <Route element={<Shell />}>
          <Route index element={<DashboardPage />} />
          <Route path="profile" element={<ProfilePage />} />
          <Route path="*" element={<NotFoundPage />} />
        </Route>
      </Route>
    </Routes>
  );
}
