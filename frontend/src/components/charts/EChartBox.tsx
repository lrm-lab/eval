import { lazy, type ComponentProps } from 'react';
import { LazyContent } from '../ui/LazyContent';

const EChartCanvas = lazy(() => import('./EChartCanvas'));

export function glassTooltipConfig(theme: 'light' | 'dark') {
  return {
    backgroundColor: theme === 'dark' ? 'rgba(17, 17, 20, 0.90)' : 'rgba(255, 255, 255, 0.92)',
    borderColor: theme === 'dark' ? '#3f3f46' : '#cbd5e1',
    borderWidth: 1,
    padding: [10, 12],
    textStyle: {
      color: theme === 'dark' ? '#f4f4f5' : '#0f172a',
      fontSize: 12,
      fontFamily: '"Inter", -apple-system, "PingFang SC", sans-serif',
    },
    extraCssText:
      'backdrop-filter: blur(10px); -webkit-backdrop-filter: blur(10px); border-radius: 10px; box-shadow: 0 12px 28px rgba(15, 23, 42, 0.18);',
  };
}

export function EChartBox(props: ComponentProps<typeof EChartCanvas>) {
  return (
    <LazyContent label="图表" height={props.height ?? 380}>
      <EChartCanvas {...props} />
    </LazyContent>
  );
}
