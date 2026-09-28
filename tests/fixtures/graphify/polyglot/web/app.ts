import { add, Counter } from "./math";

export function main(): number {
  const c = new Counter();
  c.increment();
  return add(2, 3);
}
