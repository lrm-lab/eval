import { Component, Suspense, type ReactNode } from 'react';

interface LazyContentProps {
  children: ReactNode;
  label: string;
  height?: number;
  resetKey?: string;
}

export class LazyContent extends Component<LazyContentProps, { failed: boolean }> {
  state = { failed: false };

  static getDerivedStateFromError() {
    return { failed: true };
  }

  componentDidUpdate(previous: LazyContentProps) {
    if (this.state.failed && previous.resetKey !== this.props.resetKey) {
      this.setState({ failed: false });
    }
  }

  render() {
    const { children, label, height } = this.props;
    const style = { minHeight: height };
    const className = 'flex flex-wrap items-start gap-3 p-4 text-sm text-[var(--text-secondary)]';

    if (this.state.failed) {
      return (
        <div role="alert" className={className} style={style}>
          <p>{label}加载失败，请重新加载页面。</p>
          <button
            type="button"
            onClick={() => window.location.reload()}
            className="cursor-pointer font-medium text-[var(--text-primary)] underline underline-offset-4"
          >
            重新加载页面
          </button>
        </div>
      );
    }

    return (
      <Suspense fallback={
        <p role="status" className={className} style={style}>正在加载{label}…</p>
      }>
        {children}
      </Suspense>
    );
  }
}
