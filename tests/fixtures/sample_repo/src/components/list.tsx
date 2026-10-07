import { Button } from "./dashboard";

class BaseList {
  render(): string {
    return "";
  }
}

class UserList extends BaseList {
  render(): string {
    return "";
  }
}

export function renderList() {
  return Button({ label: "go" });
}
