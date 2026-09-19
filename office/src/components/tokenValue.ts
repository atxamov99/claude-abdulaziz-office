import { currentLocale, intlTag, t } from "../i18n";

export function tokenValue(last:Record<string,number>|null|undefined,key:string):string {
  return last == null ? t("hq.tokens.noData") : new Intl.NumberFormat(intlTag(currentLocale()),{notation:"compact",maximumFractionDigits:1}).format(last[key]||0);
}
