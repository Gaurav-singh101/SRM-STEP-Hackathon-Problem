import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import React from 'react';
import { createRoot } from 'react-dom/client';

import { App } from './App';
import { ApiError } from './api/client';
import './styles.css';

/**
 * One data-fetching layer for the whole app (CLAUDE.md S7).
 *
 * Retries are conditional on purpose: a 401 or a 400 will not fix itself, and retrying
 * it three times only delays the error the user needs to see.
 */
const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      retry: (attempt, error) => {
        if (error instanceof ApiError && !error.isRetryable) return false;
        return attempt < 2;
      },
      staleTime: 30_000,
      refetchOnWindowFocus: false,
    },
  },
});

const container = document.getElementById('root');
if (!container) throw new Error('root element is missing from index.html');

createRoot(container).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <App />
    </QueryClientProvider>
  </React.StrictMode>,
);
