import { useI18n } from "./I18nContext";

interface LanguageSwitchProps {
  className?: string;
}

export function LanguageSwitch({ className = "workspace-button ww-language-switch" }: LanguageSwitchProps) {
  const { locale, toggleLocale } = useI18n();
  const label = locale === "zh" ? "切换为英文" : "Switch to Chinese";
  return (
    <button aria-label={label} className={className} onClick={toggleLocale} title={label} type="button">
      {locale === "zh" ? "EN" : "中文"}
    </button>
  );
}
