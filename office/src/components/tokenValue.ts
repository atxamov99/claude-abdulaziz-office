export function tokenValue(last:Record<string,number>|null|undefined,key:string):string {
  return last == null ? "Нет данных" : new Intl.NumberFormat("ru-RU",{notation:"compact",maximumFractionDigits:1}).format(last[key]||0);
}
