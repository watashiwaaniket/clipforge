import { SignJWT } from "jose";
import NextAuth from "next-auth";
import Credentials from "next-auth/providers/credentials";

import { authConfig, isTokenExpired, SESSION_MAX_AGE_S } from "./auth.config";

const apiUrl =
  process.env.API_URL ?? process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export const { handlers, auth, signIn, signOut } = NextAuth({
  ...authConfig,
  providers: [
    Credentials({
      credentials: {
        email: { label: "Email", type: "email" },
        password: { label: "Password", type: "password" },
      },
      async authorize(credentials) {
        const email =
          typeof credentials?.email === "string" ? credentials.email : "";
        const password =
          typeof credentials?.password === "string" ? credentials.password : "";
        if (!email || !password) {
          return null;
        }

        const response = await fetch(`${apiUrl}/api/v1/auth/login`, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ email, password }),
          cache: "no-store",
        });

        if (!response.ok) {
          return null;
        }

        const data = (await response.json()) as { id?: string; email?: string };
        if (!data.id || !data.email) {
          return null;
        }
        return { id: data.id, email: data.email };
      },
    }),
    Credentials({
      id: "guest",
      name: "Local guest",
      credentials: {},
      async authorize() {
        const response = await fetch(`${apiUrl}/api/v1/auth/guest`, {
          method: "POST",
          cache: "no-store",
        });

        if (!response.ok) {
          return null;
        }

        const data = (await response.json()) as { id?: string; email?: string };
        if (!data.id || !data.email) {
          return null;
        }
        return { id: data.id, email: data.email };
      },
    }),
  ],
  callbacks: {
    ...authConfig.callbacks,
    async jwt({ token, user }) {
      if (user) {
        const secret = process.env.JWT_SECRET;
        if (!secret) {
          throw new Error("JWT_SECRET is not configured.");
        }
        const now = Math.floor(Date.now() / 1000);
        token.sub = user.id;
        token.accessTokenExpiresAt = now + SESSION_MAX_AGE_S;
        token.accessToken = await new SignJWT({ sub: user.id })
          .setProtectedHeader({ alg: "HS256" })
          .setIssuer("clipforge")
          .setAudience("clipforge-api")
          .setIssuedAt(now)
          .setExpirationTime(now + SESSION_MAX_AGE_S)
          .sign(new TextEncoder().encode(secret));
        return token;
      }

      if (isTokenExpired(token)) {
        return null;
      }
      return token;
    },
  },
});
