import type { NextAuthConfig } from "next-auth";
import type { DefaultSession } from "next-auth";
import type { JWT } from "next-auth/jwt";

declare module "next-auth" {
  interface Session {
    accessToken?: string;
    user: DefaultSession["user"];
  }
}

declare module "next-auth/jwt" {
  interface JWT {
    accessToken?: string;
    accessTokenExpiresAt?: number;
  }
}

export const SESSION_MAX_AGE_S = 24 * 60 * 60;

export function isTokenExpired(
  token: Pick<JWT, "accessTokenExpiresAt">,
  nowSeconds: number = Date.now() / 1000
): boolean {
  return (
    typeof token.accessTokenExpiresAt !== "number" ||
    nowSeconds >= token.accessTokenExpiresAt
  );
}

export const authConfig = {
  session: { strategy: "jwt", maxAge: SESSION_MAX_AGE_S },
  pages: { signIn: "/login" },
  providers: [],
  callbacks: {
    async jwt({ token }) {
      if (isTokenExpired(token)) {
        return null;
      }
      return token;
    },
    async session({ session, token }) {
      if (typeof token.accessToken === "string") {
        session.accessToken = token.accessToken;
      }
      return session;
    },
  },
} satisfies NextAuthConfig;
