/**
 * Dashboard components: Button, forms, and admin views.
 */
import { z } from "zod";
import { cva } from "class-variance-authority";
import { prisma } from "@/lib/prisma";
import { useRouter } from "next/navigation";

/**
 * Props for the button component.
 */
interface ButtonProps {
  label: string;
  onClick?: () => void;
}

type UserId = string;

export const userSchema = z.object({
  id: z.string(),
  name: z.string(),
});

export const buttonVariants = cva("inline-flex items-center", {
  variants: {
    variant: {
      default: "bg-primary",
      outline: "border",
    },
  },
});

/**
 * Renders a styled button.
 */
export const Button = (props: ButtonProps) => {
  const local = useRouter();
  const handleClick = () => {
    props.onClick?.();
  };

  return handleClick;
};

export async function createUser(input: z.infer<typeof userSchema>) {
  return prisma.user.create({ data: input });
}

export default function DashboardPage() {
  return null;
}

class ApiClient {
  async get(url: string) {
    return fetch(url);
  }
}

enum Role {
  Admin,
  User,
}
