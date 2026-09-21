<%@ Page Language="C#" AutoEventWireup="true" CodeBehind="Default.aspx.cs" Inherits="MyApp.Web.Default" ValidateRequest="false" %>
<%@ Register TagPrefix="uc" TagName="Header" Src="~/Controls/Header.ascx" %>
<%@ Import Namespace="MyApp.Web.Helpers" %>
<!DOCTYPE html>
<html>
<head runat="server"><title>Home</title></head>
<body>
    <form id="form1" runat="server">
        <asp:Label ID="lblWelcome" runat="server" Text="Welcome" />
        <asp:GridView ID="gvOrders" runat="server" />
        <% Response.Write("Hello " + Request["name"]); %>
        <span><%= DateTime.Now %></span>
    </form>
</body>
</html>
