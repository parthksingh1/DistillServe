import { createBrowserRouter, RouterProvider } from 'react-router-dom';
import type { ReactElement } from 'react';

import { Shell } from '@/components/Shell';
import { Adapters } from '@/pages/Adapters';
import { Dashboard } from '@/pages/Dashboard';
import { Evals } from '@/pages/Evals';
import { Overview } from '@/pages/Overview';
import { Playground } from '@/pages/Playground';
import { Registry } from '@/pages/Registry';
import { Rollouts } from '@/pages/Rollouts';
import { Traces } from '@/pages/Traces';

/**
 * Routes are declared once, here, and the nav in `Shell` mirrors them. Both
 * lists are short enough that keeping them in sync by hand is cheaper than the
 * indirection a shared config would add.
 */
const router = createBrowserRouter([
  {
    path: '/',
    element: <Shell />,
    children: [
      { index: true, element: <Overview /> },
      { path: 'playground', element: <Playground /> },
      { path: 'dashboard', element: <Dashboard /> },
      { path: 'adapters', element: <Adapters /> },
      { path: 'rollouts', element: <Rollouts /> },
      { path: 'evals', element: <Evals /> },
      { path: 'traces', element: <Traces /> },
      { path: 'registry', element: <Registry /> },
    ],
  },
]);

export function App(): ReactElement {
  return <RouterProvider router={router} />;
}
