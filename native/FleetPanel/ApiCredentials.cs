using System;
using System.Runtime.InteropServices;
using System.Text;

namespace FleetPanel
{
    /// <summary>Keys stay in the current user's Windows Credential Manager.</summary>
    internal static class ApiCredentials
    {
        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct Credential
        {
            public uint Flags, Type;
            public string TargetName, Comment;
            public System.Runtime.InteropServices.ComTypes.FILETIME LastWritten;
            public uint CredentialBlobSize;
            public IntPtr CredentialBlob;
            public uint Persist, AttributeCount;
            public IntPtr Attributes;
            public string TargetAlias, UserName;
        }

        [DllImport("advapi32.dll", EntryPoint = "CredWriteW", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CredWrite(ref Credential credential, uint flags);

        [DllImport("advapi32.dll", EntryPoint = "CredDeleteW", CharSet = CharSet.Unicode, SetLastError = true)]
        [return: MarshalAs(UnmanagedType.Bool)]
        private static extern bool CredDelete(string target, uint type, uint flags);

        public static string Save(string id, string key)
        {
            if (!KnownAccount(id)) return "Unknown account.";
            key = (key ?? "").Trim();
            if (key.Length == 0 || key.Length > 1024 || Array.Exists(key.ToCharArray(), char.IsWhiteSpace))
                return "Enter a valid key without spaces.";
            var bytes = Encoding.Unicode.GetBytes(key);
            var memory = Marshal.AllocHGlobal(bytes.Length);
            try
            {
                Marshal.Copy(bytes, 0, memory, bytes.Length);
                var credential = new Credential
                {
                    Type = 1, TargetName = "Fleet/api/" + id,
                    CredentialBlobSize = (uint)bytes.Length, CredentialBlob = memory,
                    Persist = 2, UserName = "Fleet",
                };
                return CredWrite(ref credential, 0) ? "" : "Windows could not save the key. Try again.";
            }
            finally
            {
                Array.Clear(bytes, 0, bytes.Length);
                for (var i = 0; i < key.Length * 2; i++) Marshal.WriteByte(memory, i, 0);
                Marshal.FreeHGlobal(memory);
            }
        }

        public static string Remove(string id)
        {
            if (!KnownAccount(id)) return "Unknown account.";
            return CredDelete("Fleet/api/" + id, 1, 0) || Marshal.GetLastWin32Error() == 1168
                ? "" : "Windows could not remove the key. Try again.";
        }

        private static bool KnownAccount(string id) => id == "anthropic" || id == "anthropic-admin"
            || id == "anthropic-backup" || id == "anthropic-backup-admin";
    }
}
