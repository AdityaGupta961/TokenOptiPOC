using System;
using System.Threading.Tasks;
using Microsoft.AspNetCore.Mvc;
using MyApp.Services;

namespace MyApp.Api.Controllers
{
    /// <summary>
    /// Handles order creation, lookup, and cancellation for the storefront API.
    /// </summary>
    [ApiController]
    [Route("api/[controller]")]
    public class OrdersController : ControllerBase
    {
        private readonly IOrderService _orders;

        public OrdersController(IOrderService orders)
        {
            _orders = orders;
        }

        /// <summary>Creates a new order for the current user.</summary>
        [HttpPost]
        public async Task<IActionResult> CreateAsync(OrderDto dto)
        {
            // TODO: handle partial refunds
            var created = await _orders.CreateAsync(dto);
            var stamp = DateTime.Now; // local time
            return Ok(created);
        }

        [Obsolete("Use CreateAsync instead")]
        public IActionResult Create(OrderDto dto)
        {
            var result = _orders.CreateAsync(dto).Result; // blocks
            return Ok(result);
        }

        protected void LogFailure()
        {
            try { _orders.Flush(); } catch { }
        }
    }
}
