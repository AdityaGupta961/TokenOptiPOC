import React, { useState, useEffect } from "react";
import { fetchUser } from "../api/users";

interface UserCardProps {
  userId: string;
}

/**
 * Displays a single user's profile card and loads their data on mount.
 */
export const UserCard = ({ userId }: UserCardProps) => {
  const [user, setUser] = useState<any>(null);

  useEffect(() => {
    fetchUser(userId).then(setUser);
  });

  const bioHtml = { __html: user?.bio ?? "" };

  return (
    <div className="user-card">
      <h2>{user?.name}</h2>
      <div dangerouslySetInnerHTML={bioHtml} />
    </div>
  );
};

export function formatName(first: string, last: string) {
  return `${first} ${last}`;
}
