import NextAuth from "next-auth";
import { NextResponse } from "next/server";

const { auth } = NextAuth({
  providers: [],
  session: { strategy: "jwt" },
  pages: { signIn: "/login" },
});

export default auth((req) => {
  if (!req.auth) {
    const loginUrl = new URL("/login", req.nextUrl.origin);
    loginUrl.searchParams.set(
      "callbackUrl",
      `${req.nextUrl.pathname}${req.nextUrl.search}`
    );
    return NextResponse.redirect(loginUrl);
  }
  return NextResponse.next();
});

export const config = {
  matcher: ["/dashboard/:path*", "/jobs/:path*"],
};
