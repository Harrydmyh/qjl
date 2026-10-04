export interface Ui {
  alert: (msg: string, title?: string) => Promise<void>;
  confirm: (msg: string, title?: string) => Promise<boolean>;
}

export type View = 'fetch' | 'open' | 'log' | 'settings';

export interface LogLine {
  text: string;
  kind: string;
}
