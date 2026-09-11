import { Component } from "react";
import type { ReactNode, ErrorInfo } from "react";
import { BrandMark } from "./common/BrandMark";
import { Icon } from "./ui/Icon";
import { LanguageSwitch } from "../i18n/LanguageSwitch";

interface Props {
  children: ReactNode;
  fallback?: ReactNode;
}

interface State {
  hasError: boolean;
  error: Error | null;
}

export default class ErrorBoundary extends Component<Props, State> {
  constructor(props: Props) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): State {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("ErrorBoundary caught:", error, info.componentStack);
  }

  render() {
    if (this.state.hasError) {
      if (this.props.fallback) return this.props.fallback;
      return (
        <main className="ww-auth-shell">
          <LanguageSwitch className="workspace-button ww-auth-language" />
          <section className="ww-error-card" role="alert">
            <BrandMark description="参数化建模工作台" nameAs="h1" size="login" />
            <span aria-hidden="true" className="ww-error-icon"><Icon name="shield-check" size={20} /></span>
            <h2>渲染出错</h2>
            <p>当前界面遇到错误。你的工程会话仍保存在本地，可以安全重试。</p>
            <code data-i18n-skip>{this.state.error?.message || "未知错误"}</code>
            <button
              className="workspace-button workspace-button--primary"
              onClick={() => this.setState({ hasError: false, error: null })}
            >
              重试
            </button>
          </section>
        </main>
      );
    }
    return this.props.children;
  }
}
