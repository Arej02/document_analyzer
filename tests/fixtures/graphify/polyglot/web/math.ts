export function add(a: number, b: number): number {
  return a + b;
}

export class Counter {
  count = 0;
  increment(): number {
    this.count = add(this.count, 1);
    return this.count;
  }
}
