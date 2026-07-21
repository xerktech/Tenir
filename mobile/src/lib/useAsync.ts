/**
 * A tiny load-and-reload hook so screens don't each reimplement fetch/loading/error.
 *
 * Identical in spirit to the web SPA's `useAsync` (master plan §8.2 — write the data
 * logic once, render per platform). It depends only on `react`, never on
 * `react-native`, so the container hooks built on it stay unit-testable under vitest.
 */

import { useCallback, useEffect, useState } from "react";

export interface AsyncState<T> {
  data: T | null;
  error: unknown;
  loading: boolean;
  reload: () => void;
}

export function useAsync<T>(loader: () => Promise<T>, deps: unknown[] = []): AsyncState<T> {
  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<unknown>(null);
  const [loading, setLoading] = useState(true);

  const reload = useCallback(() => {
    setLoading(true);
    loader()
      .then((d) => {
        setData(d);
        setError(null);
      })
      .catch((e) => setError(e))
      .finally(() => setLoading(false));
    // The loader closes over the caller's dependencies, captured here intentionally.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => reload(), [reload]);

  return { data, error, loading, reload };
}
