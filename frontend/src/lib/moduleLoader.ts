import { useEffect, useState } from 'react';

type ModuleResult<T> = { value: T } | { error: unknown };

// Keep the loading UI explicit so a ready module can render as soon as it resolves.
export function createModuleLoader<T>(load: () => Promise<T>) {
  let pending: Promise<ModuleResult<T>> | undefined;
  let result: ModuleResult<T> | undefined;

  function preload() {
    pending ??= load().then(
      (value) => {
        result = { value };
        return result;
      },
      (error: unknown) => {
        result = { error };
        return result;
      },
    );
    return pending;
  }

  function useModule() {
    const [loaded, setLoaded] = useState(result);

    useEffect(() => {
      if (loaded) return;
      let active = true;
      void preload().then((value) => {
        if (active) setLoaded(value);
      });
      return () => {
        active = false;
      };
    }, [loaded]);

    if (loaded && 'error' in loaded) throw loaded.error;
    return loaded?.value;
  }

  return { preload, useModule };
}
