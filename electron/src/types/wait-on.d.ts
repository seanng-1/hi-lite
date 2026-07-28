declare module 'wait-on' {
  export interface WaitOnOptions {
    resources: string[];
    delay?: number;
    interval?: number;
    simultaneous?: number;
    timeout?: number;
    validateStatus?: (status: number) => boolean;
    log?: boolean;
    reverse?: boolean;
    strictSSL?: boolean;
    tcpTimeout?: number;
  }

  function waitOn(options: WaitOnOptions): Promise<void>;
  export default waitOn;
}


